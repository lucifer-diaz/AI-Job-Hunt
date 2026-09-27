"""Scheduled jobs: the work behind every scheduler entry, manual admin trigger and cron run.

Each function receives a JobContext, opens its own database sessions, handles a bounded amount of
work and returns a JSON-serialisable summary. A summary whose `skipped` value is a string means the
job had nothing it was allowed to do; that is not an error.

Work that belongs to a person runs per account, in a session bound to that account (app.tenancy),
with that account's own API keys and preferences: its search schedule, auto-ranking switch and AI
provider. One account failing never stops the others.

Every job is safe to run twice, including at the same moment from two app instances:
- row-level work is claimed with PostgreSQL locks (ranking task and work_queue leases, FOR UPDATE SKIP LOCKED);
- finished work is detected from the database before acting (ranking tasks and inputs, compensation_checked_at,
  SearchRun.completed_at, existing follow-up drafts, a search run started within the interval).
"""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Application, Job, SearchRun, User
from app.services import (
    ai_advisor,
    external_monitoring,
    job_description,
    monitoring,
    ranker,
    system_log,
)
from app.services import work_queue as wq
from app.services.compensation import enrich_compensation
from app.services.hunter import run_due_profiles
from app.services.lifecycle import WorkflowState as S
from app.services.sources import give_up_on_run, refresh_apify_run
from app.services.user_settings import ai_ready, apify_token
from app.services.workflow import create_due_followups
from app.settings import get_settings
from app.workspace import run_due_saved_searches

SessionFactory = Callable[[], Session]
# Jobs not ranked yet: before the ranker's hard filters, waiting for it, or being ranked.
RANK_PENDING_STATES = ranker.RANK_WAITING_STATES


@dataclass(frozen=True)
class JobContext:
    sessions: SessionFactory
    trigger: str  # schedule | manual | cron
    now: datetime
    worker_id: str
    interval_minutes: int


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    interval_setting: str
    run: Callable[[JobContext], dict[str, Any] | Awaitable[dict[str, Any]]]
    description: str


def skipped(reason: str, **details: Any) -> dict[str, Any]:
    return {"skipped": reason, **details}


def _is_postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _as_user(db: Session, user_id: int) -> Session:
    """Bind a session to one account so it can only see and change that account's data."""
    db.info["user_id"] = user_id
    return db


def _account_ids(ctx: JobContext) -> list[int]:
    with ctx.sessions() as db:
        return list(db.scalars(select(User.id).order_by(User.id)).all())


def _error(user_id: int, exc: Exception) -> dict[str, Any]:
    return {"user_id": user_id, "error": f"{type(exc).__name__}: {exc}"[:300]}


# ───────────── hunter / Apify ─────────────

def _hunt_for_account(ctx: JobContext, db: Session) -> tuple[list[int] | str, list[dict[str, Any]]]:
    """Start this account's due saved Discovery searches and its due Hunter search profiles.

    A manual trigger runs every scheduled setup at once, whatever their next run time says.
    """
    if not apify_token(db):
        return "no Apify key", []
    setups = run_due_saved_searches(db, db.info["user_id"], ctx.now, force=ctx.trigger == "manual")
    profiles = run_due_profiles(db, db.info["user_id"], ctx.now)
    started = setups["run_ids"] + profiles["run_ids"]
    errors = setups["errors"] + profiles["errors"]
    if not started and not errors:
        return "no search was due", []
    return started, errors


def hunter(ctx: JobContext) -> dict[str, Any]:
    """Start each account's due searches, with its own Apify token, at most once per interval."""
    accounts = _account_ids(ctx)
    if not accounts:
        return skipped("No accounts yet")
    started, not_started, errors = 0, Counter(), []
    for user_id in accounts:
        try:
            with _as_user(ctx.sessions(), user_id) as db:
                outcome, profile_errors = _hunt_for_account(ctx, db)
        except Exception as exc:  # one account's failure must not stop the others
            errors.append(_error(user_id, exc))
            continue
        errors += [{"user_id": user_id, **problem} for problem in profile_errors]
        if isinstance(outcome, str):
            not_started[outcome] += 1
        else:
            started += len(outcome)
    summary = {"accounts": len(accounts), "started": started, "not_started": dict(not_started), "errors": errors}
    if not started and not errors:
        return skipped("No account had a search due", **summary)
    return summary


def apify_sync(ctx: JobContext) -> dict[str, Any]:
    """Check unfinished Apify runs of every account and import the datasets of the ones that finished."""
    summary: dict[str, Any] = {"checked": 0, "completed": 0, "created": 0, "no_token": 0, "gave_up": 0, "errors": []}
    max_age = timedelta(hours=max(1, get_settings().apify_run_max_age_hours))
    with ctx.sessions() as db:
        pending = db.execute(select(SearchRun.id, SearchRun.user_id)
                             .where(SearchRun.provider == "apify", SearchRun.completed_at.is_(None))
                             .order_by(SearchRun.id).limit(max(1, get_settings().apify_sync_batch_size))).all()
        tokens: dict[int, bool] = {}
        for run_id, owner in pending:
            if owner is not None and owner not in tokens:
                tokens[owner] = bool(apify_token(db, owner))
            if owner is None or not tokens[owner]:
                summary["no_token"] += 1
                continue
            run = db.scalars(select(SearchRun).where(SearchRun.id == run_id, SearchRun.completed_at.is_(None))
                             .with_for_update(skip_locked=True)).first()
            if run is None:
                continue  # finished meanwhile, or another instance is importing it right now
            if run.created_at and ctx.now - run.created_at > max_age:
                # Checking forever is what kept stale runs hammering Apify: no run lasts this long.
                give_up_on_run(db, run, f"Still unfinished {max_age.total_seconds() / 3600:.0f} hours after it "
                                        "started, so it is no longer checked")
                summary["gave_up"] += 1
                continue
            try:
                out = refresh_apify_run(db, run)
            except Exception as exc:  # one bad run must not stop the others
                db.rollback()
                summary["errors"].append({"run_id": run_id, "error": str(exc)[:300]})
                continue
            summary["checked"] += 1
            summary["gave_up"] += int(bool(out.get("gave_up")))
            summary["completed"] += int(bool(out.get("completed")))
            summary["created"] += (out.get("import") or {}).get("created", 0)
    return summary


# ───────────── ranking ─────────────

def _prepare_ranking(ctx: JobContext) -> tuple[list[ranker.TaskClaim], dict[str, Any]] | str:
    """Deterministic sweep (filter, queue, recover, re-rank), then claim a bounded batch of ranking tasks."""
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "Ranking workers need PostgreSQL (FOR UPDATE SKIP LOCKED)"
        queue = ranker.sweep(db)
        ready, auto, waiting = ranker.accounts(db, ctx.now)
        claims = ranker.claim(db, worker_id=ctx.worker_id, limit=max(1, get_settings().ranking_batch_size),
                              ready_accounts=ready, auto_rank_accounts=auto)
    return claims, {"queue": queue, "accounts_waiting": waiting}


async def ranking(ctx: JobContext) -> dict[str, Any]:
    """Claim a limited batch of ranking tasks and run at most RANKING_CONCURRENCY model calls at a time."""
    prepared = await asyncio.to_thread(_prepare_ranking, ctx)
    if isinstance(prepared, str):
        return skipped(prepared)
    claims, details = prepared
    semaphore = asyncio.Semaphore(max(1, get_settings().ranking_concurrency))

    async def run(claimed: ranker.TaskClaim) -> str:
        async with semaphore:
            return await asyncio.to_thread(ranker.process, ctx.sessions, claimed)

    outcomes = Counter(await asyncio.gather(*(run(c) for c in claims)))
    summary = {"claimed": len(claims), **{name: outcomes.get(name, 0) for name in ranker.OUTCOMES}, **details}
    if not claims and not any(details["queue"].values()):
        return skipped("Nothing to rank", **summary)
    return summary


def _prepare_jd_parsing(ctx: JobContext) -> tuple[list[job_description.ParseClaim], dict[str, Any]] | str:
    """Queue postings that were never parsed, recover crashed workers, then claim a bounded batch."""
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "Job description parsing needs PostgreSQL (FOR UPDATE SKIP LOCKED)"
        recovered = job_description.recover_expired(db)
        queue = job_description.sweep(db)
        claims = job_description.claim(db, worker_id=ctx.worker_id,
                                       limit=max(1, get_settings().jd_parse_batch_size))
    return claims, {"queue": queue, "recovered": recovered}


async def jd_parsing(ctx: JobContext) -> dict[str, Any]:
    """Parse a bounded batch of job descriptions into structured requirements, a few model calls at a time."""
    if not get_settings().jd_parse_enabled:
        return skipped("Job description parsing is off (JD_PARSE_ENABLED)")
    prepared = await asyncio.to_thread(_prepare_jd_parsing, ctx)
    if isinstance(prepared, str):
        return skipped(prepared)
    claims, details = prepared
    semaphore = asyncio.Semaphore(max(1, get_settings().jd_parse_concurrency))

    async def run(claim: job_description.ParseClaim) -> str:
        async with semaphore:
            return await asyncio.to_thread(job_description.process, ctx.sessions, claim)

    outcomes = Counter(await asyncio.gather(*(run(claim) for claim in claims)))
    summary = {"claimed": len(claims), **{name: outcomes.get(name, 0) for name in job_description.OUTCOMES}, **details}
    if not claims and not details["queue"]["queued"]:
        return skipped("No job descriptions waiting", **summary)
    return summary


# ───────────── enrichment, applications, follow-ups, inbox ─────────────

def compensation(ctx: JobContext) -> dict[str, Any]:
    """Record the salary each posting states. Glassdoor and PayScale are marked not configured.

    Rows are read without locks and written with a guarded UPDATE, so enrichment never holds row locks
    while it works (which would make the ranking claim skip those jobs), and a job checked by another
    instance in the meantime is not written twice.
    """
    with ctx.sessions() as db:
        rows = db.execute(select(Job.id, Job.raw).where(Job.compensation_checked_at.is_(None)).order_by(Job.id)
                          .limit(max(1, get_settings().compensation_batch_size))).all()
        results = [(job_id, enrich_compensation(raw, ctx.now)) for job_id, raw in rows]
        checked = found = 0
        for job_id, data in results:
            written = db.execute(update(Job).where(Job.id == job_id, Job.compensation_checked_at.is_(None))
                                 .values(compensation=data, compensation_checked_at=ctx.now)
                                 .execution_options(synchronize_session=False)).rowcount
            checked += written
            found += written and data["status"] == "found"
        db.commit()
        remaining = db.scalar(select(func.count(Job.id)).where(Job.compensation_checked_at.is_(None))) or 0
    return {"checked": checked, "salary_found": found, "remaining": remaining}


def applications(ctx: JobContext) -> dict[str, Any]:
    """Report the application queue. Submission stays a human action, so nothing is submitted here."""
    with ctx.sessions() as db:
        counts = dict(db.execute(select(Job.state, func.count()).where(Job.state.in_((S.READY_TO_APPLY, S.APPLYING)))
                                 .group_by(Job.state)).all())
    return skipped("Automatic application submission is not enabled; approved packets stay in the apply kit",
                   ready_to_apply=counts.get(S.READY_TO_APPLY, 0), applying=counts.get(S.APPLYING, 0))


def followups(ctx: JobContext) -> dict[str, Any]:
    """Draft each account's due follow-ups with its own AI key. Drafts only; nothing is sent."""
    accounts = _account_ids(ctx)
    drafted, no_ai_key, errors = 0, 0, []
    for user_id in accounts:
        try:
            with _as_user(ctx.sessions(), user_id) as db:
                if not ai_ready(db, user_id)[0]:
                    no_ai_key += 1
                    continue
                drafted += create_due_followups(db)
        except Exception as exc:
            errors.append(_error(user_id, exc))
    summary = {"accounts": len(accounts), "drafted": drafted, "no_ai_key": no_ai_key, "errors": errors}
    if not accounts:
        return skipped("No accounts yet", **summary)
    return summary


def inbox(ctx: JobContext) -> dict[str, Any]:
    return skipped("No email provider is configured, so there is no inbox to sync yet")


# ───────────── analytics, cleanup ─────────────

def analytics(ctx: JobContext) -> dict[str, Any]:
    """Snapshot of the whole system, kept as this job's last result (visible to the admin only)."""
    with ctx.sessions() as db:
        count = lambda *where: db.scalar(select(func.count()).select_from(Job).where(*where)) or 0
        by_state = {str(state): n for state, n in db.execute(select(Job.state, func.count()).group_by(Job.state)).all()}
        by_status = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        by_stage = {stage or "applied": n for stage, n in db.execute(
            select(Application.stage, func.count()).where(Application.applied_at.is_not(None))
            .group_by(Application.stage)).all()}
        pending = {"ranking": count(Job.state.in_(RANK_PENDING_STATES)),
                   "compensation": count(Job.compensation_checked_at.is_(None)),
                   "search_runs": db.scalar(select(func.count(SearchRun.id)).where(SearchRun.completed_at.is_(None))) or 0}
        failed = count(Job.state == S.FAILED)
        accounts = db.scalar(select(func.count(User.id))) or 0
    return {"accounts": accounts, "jobs_by_state": by_state, "jobs_by_status": by_status,
            "applications_by_stage": by_stage, "pending": pending, "failed_jobs": failed,
            "generated_at": ctx.now.isoformat()}


def cleanup(ctx: JobContext) -> dict[str, Any]:
    """Recover work whose worker crashed, and apply telemetry retention (roll up, then delete old rows).

    Three retentions, each with its own setting: LLM requests and external API calls are rolled into daily
    totals that are kept for good, while operational log lines are simply deleted.
    """
    with ctx.sessions() as db:
        telemetry = monitoring.prune(db, ctx.now)
        external = external_monitoring.prune(db, ctx.now)
        logs = system_log.prune(db, ctx.now)
        retention = {"telemetry": telemetry, "external_api": external, "system_logs": logs}
        if not _is_postgres(db):
            return skipped("Lease recovery needs PostgreSQL", **retention)
        return {"recovered_leases": wq.recover_expired_leases(db), **retention}


def litellm_telemetry(ctx: JobContext) -> dict[str, Any]:
    """Fill in the auto router's tier and cause from LiteLLM's spend logs (best effort, bounded)."""
    from app.services.litellm_routing_import import import_routing

    result = import_routing(ctx.sessions, ctx.now)
    if not result["accounts"]:
        return skipped("No auto-router requests waiting for LiteLLM routing details", **result)
    return result


ADVISOR_BATCH = 5


def ai_optimization(ctx: JobContext) -> dict[str, Any]:
    """Queue scheduled analyses, then process a few waiting AI optimization tasks. Recommendations only."""
    with ctx.sessions() as db:
        queued = ai_advisor.enqueue_scheduled(db, ctx.now)
        recovered = ai_advisor.recover_expired(db, ctx.now)
    outcomes: Counter = Counter()
    for _ in range(ADVISOR_BATCH):
        claim = ai_advisor.claim_next(ctx.sessions, ctx.worker_id)
        if claim is None:
            break
        outcomes[ai_advisor.process(ctx.sessions, claim)] += 1
    summary = {"scheduled": queued, "recovered": recovered, **{k: outcomes.get(k, 0) for k in
                                                               ("completed", "retrying", "failed", "lost")}}
    if not queued and not recovered and not outcomes:
        return skipped("No analyses waiting", **summary)
    return summary


JOBS: list[JobSpec] = [
    JobSpec("hunter", "Hunter / Apify search", "hunter_interval_minutes", hunter,
            "Starts each account's saved search when its schedule is not manual, with its own Apify key."),
    JobSpec("apify_sync", "Apify result sync", "apify_sync_interval_minutes", apify_sync,
            "Imports datasets of Apify runs that finished."),
    JobSpec("ranking", "Job ranking", "ranking_interval_minutes", ranking,
            "Filters and queues new jobs, then ranks a bounded batch of ranking tasks, a few at a time."),
    JobSpec("jd_parsing", "Job description parsing", "jd_parse_interval_minutes", jd_parsing,
            "Parses job descriptions into structured requirements, each tied to the sentence it came from."),
    JobSpec("compensation", "Salary enrichment", "compensation_interval_minutes", compensation,
            "Records the salary stated in each posting."),
    JobSpec("applications", "Application processing", "application_interval_minutes", applications,
            "Reports the application queue; submission is not automated."),
    JobSpec("followups", "Follow-ups", "followup_interval_minutes", followups,
            "Drafts due follow-ups without sending them."),
    JobSpec("inbox", "Inbox sync", "inbox_interval_minutes", inbox,
            "Reserved for an email provider; currently not configured."),
    JobSpec("analytics", "Analytics", "analytics_interval_minutes", analytics,
            "Stores a pipeline snapshot."),
    JobSpec("ai_optimization", "AI optimization advisor", "ai_optimization_interval_minutes", ai_optimization,
            "Analyses LLM usage and writes recommendations; never changes configuration."),
    JobSpec("litellm_telemetry", "LiteLLM routing import", "litellm_telemetry_interval_minutes", litellm_telemetry,
            "Adds the auto router's tier and cause from LiteLLM's spend logs to Monitoring."),
    JobSpec("cleanup", "Cleanup", "cleanup_interval_minutes", cleanup,
            "Recovers jobs left locked by crashed workers and applies LLM telemetry retention."),
]
