"""Scheduled jobs: the work behind every scheduler entry, manual admin trigger and cron run.

Each function receives a JobContext, opens its own database sessions, handles a bounded amount of
work and returns a JSON-serialisable summary. A summary whose `skipped` value is a string means the
job had nothing it was allowed to do; that is not an error.

Work that belongs to a person runs per account, in a session bound to that account (app.tenancy),
with that account's own API keys and preferences: its search schedule, auto-ranking switch and AI
provider. One account failing never stops the others.

Every job is safe to run twice, including at the same moment from two app instances:
- row-level work is claimed with PostgreSQL locks (work_queue leases or FOR UPDATE SKIP LOCKED);
- finished work is detected from the database before acting (fit_score, compensation_checked_at,
  SearchRun.completed_at, existing follow-up drafts, a search run started within the interval).
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Application, Job, SearchRun, User
from app.services import work_queue as wq
from app.services.ai_config import resolve as resolve_ai_config
from app.services.compensation import enrich_compensation
from app.services.lifecycle import ErrorCategory
from app.services.lifecycle import WorkflowState as S
from app.services.llm import LLMError
from app.services.hunter import run_due_profiles
from app.services.sources import refresh_apify_run
from app.services.user_settings import KEY_LABELS, ai_overrides, apify_token, profile_for, user_keys
from app.services.workflow import create_due_followups, rank_job
from app.settings import get_settings
from app.workspace import get_search_profile, run_search_profile

SessionFactory = Callable[[], Session]
# Jobs waiting for ranking: the deterministic normalize and filter steps run first, then the LLM.
RANK_PENDING_STATES = (S.DISCOVERED, S.NORMALIZED, S.FILTERED)
RANK_OUTCOMES = ("ranked", "filtered_out", "retrying", "failed", "released", "lost")
CONFIG_ERROR = re.compile(r"is missing|REQUIRE_FREE|Unsupported LLM_PROVIDER", re.I)


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


def ai_ready(db: Session, user_id: int) -> tuple[bool, str]:
    """Whether this account's chosen AI provider can be called, so workers never burn attempts on a missing key."""
    provider = resolve_ai_config(get_settings(), ai_overrides(db, user_id))["llm_provider"]
    if provider == "mock" or user_keys(db, user_id).get(provider):
        return True, ""
    return False, f"No {KEY_LABELS.get(provider, provider)} API key saved in Settings"


# ───────────── hunter / Apify ─────────────

def _saved_search(ctx: JobContext, db: Session) -> list[int] | str:
    """The Discovery screen's saved search profile (the older, single-profile one)."""
    profile = get_search_profile(db)
    if ctx.trigger != "manual" and profile.get("schedule") == "manual":
        return "schedule is manual"
    if not profile.get("roles"):
        return "no roles in search profile"
    latest = db.scalar(select(func.max(SearchRun.created_at)).where(SearchRun.adapter.is_(None)))
    if latest and latest > ctx.now - timedelta(minutes=max(1, ctx.interval_minutes)):
        return "searched recently"
    try:
        return [run.id for run in run_search_profile(db, profile)]
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc


def _hunt_for_account(ctx: JobContext, db: Session) -> tuple[list[int] | str, list[dict[str, Any]]]:
    """Start this account's due Hunter search profiles and its saved Discovery search."""
    if not apify_token(db):
        return "no Apify key", []
    saved = _saved_search(ctx, db)
    profiles = run_due_profiles(db, db.info["user_id"], ctx.now)
    started = profiles["run_ids"] + (saved if isinstance(saved, list) else [])
    if not started and isinstance(saved, str):
        return saved, profiles["errors"]
    return started, profiles["errors"]


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
    summary: dict[str, Any] = {"checked": 0, "completed": 0, "created": 0, "no_token": 0, "errors": []}
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
            try:
                out = refresh_apify_run(db, run)
            except Exception as exc:  # one bad run must not stop the others
                db.rollback()
                summary["errors"].append({"run_id": run_id, "error": str(exc)[:300]})
                continue
            summary["checked"] += 1
            summary["completed"] += int(bool(out.get("completed")))
            summary["created"] += (out.get("import") or {}).get("created", 0)
    return summary


# ───────────── ranking ─────────────

def filter_reason(db: Session, job: Job) -> str | None:
    if job.status == "dismissed":
        return "Dismissed in triage"
    preferences = profile_for(db, job.user_id).search_preferences or {}
    excluded = {str(c).strip().lower() for c in preferences.get("exclude_companies") or [] if str(c).strip()}
    if (job.company or "").strip().lower() in excluded:
        return f"{job.company} is an excluded company"
    return None


def _record_failure(db: Session, claimed: wq.Claim, exc: Exception) -> str:
    try:
        if isinstance(exc, LLMError) and CONFIG_ERROR.search(str(exc)):
            wq.release(db, claimed)  # configuration problem: hand the job back without using an attempt
            return "released"
        category = ErrorCategory.EXTERNAL_SERVICE if isinstance(exc, LLMError) else ErrorCategory.UNKNOWN
        job = wq.fail(db, claimed, category, f"{type(exc).__name__}: {exc}")
        return "failed" if job.state is S.FAILED else "retrying"
    except wq.LeaseLost:
        return "lost"


def rank_claimed_job(sessions: SessionFactory, claimed: wq.Claim) -> str:
    """Normalize, filter and rank one claimed job as its owner, committing each step on its own."""
    current = claimed
    with sessions() as db:
        try:
            job = db.get(Job, claimed.job_id)
            if job is None or job.user_id is None:
                return "lost"
            _as_user(db, job.user_id)  # the owner's profile, evidence and AI key from here on
            if job.state is S.DISCOVERED:
                if not (job.title or "").strip() or not (job.company or "").strip():
                    wq.complete(db, current, S.SKIPPED, reason="Missing title or company")
                    return "filtered_out"
                current = wq.advance(db, current, S.NORMALIZED, reason="Title and company present")
            if job.state is S.NORMALIZED:
                reason = filter_reason(db, job)
                if reason:
                    wq.complete(db, current, S.SKIPPED, reason=reason)
                    return "filtered_out"
                current = wq.advance(db, current, S.FILTERED, reason="Passed search filters")
            if job.fit_score is None:  # already-ranked jobs (e.g. from the dashboard) are not re-billed
                rank_job(db, job)
            wq.complete(db, current, S.RANKED, reason=f"Fit score {job.fit_score:.0f}")
            return "ranked"
        except wq.LeaseLost:
            return "lost"
        except Exception as exc:
            db.rollback()
            return _record_failure(db, current, exc)


def ranking_accounts(db: Session) -> list[int]:
    """Accounts whose jobs may be ranked now: auto-ranking on and a usable AI key saved."""
    eligible = []
    for user_id in db.scalars(select(User.id).order_by(User.id)).all():
        search = (profile_for(db, user_id).search_preferences or {}).get("search_profile") or {}
        if search.get("auto_rank", True) and ai_ready(db, user_id)[0]:
            eligible.append(user_id)
    return eligible


def _claim_rank_batch(ctx: JobContext) -> list[wq.Claim] | str:
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return "Ranking workers need PostgreSQL (FOR UPDATE SKIP LOCKED)"
        accounts = ranking_accounts(db)  # also creates missing profiles here, not from several threads at once
        if not accounts:
            return "No account has auto-ranking on with an AI key saved"
        return wq.claim(db, RANK_PENDING_STATES, worker_id=ctx.worker_id,
                        limit=max(1, get_settings().ranking_batch_size), where=[Job.user_id.in_(accounts)])


async def ranking(ctx: JobContext) -> dict[str, Any]:
    """Claim a limited batch of jobs waiting for ranking and process a few at a time."""
    claims = await asyncio.to_thread(_claim_rank_batch, ctx)
    if isinstance(claims, str):
        return skipped(claims)
    semaphore = asyncio.Semaphore(max(1, get_settings().ranking_concurrency))

    async def process(claimed: wq.Claim) -> str:
        async with semaphore:
            return await asyncio.to_thread(rank_claimed_job, ctx.sessions, claimed)

    outcomes = Counter(await asyncio.gather(*(process(c) for c in claims)))
    return {"claimed": len(claims), **{name: outcomes.get(name, 0) for name in RANK_OUTCOMES}}


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
        count = lambda *where: db.scalar(select(func.count()).select_from(Job).where(*where)) or 0  # noqa: E731
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
    """Recover work whose worker crashed: expired leases are retried or failed."""
    with ctx.sessions() as db:
        if not _is_postgres(db):
            return skipped("Lease recovery needs PostgreSQL")
        return {"recovered_leases": wq.recover_expired_leases(db)}


JOBS: list[JobSpec] = [
    JobSpec("hunter", "Hunter / Apify search", "hunter_interval_minutes", hunter,
            "Starts each account's saved search when its schedule is not manual, with its own Apify key."),
    JobSpec("apify_sync", "Apify result sync", "apify_sync_interval_minutes", apify_sync,
            "Imports datasets of Apify runs that finished."),
    JobSpec("ranking", "Job ranking", "ranking_interval_minutes", ranking,
            "Normalizes, filters and ranks waiting jobs of accounts with auto-ranking on, a few at a time."),
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
    JobSpec("cleanup", "Cleanup", "cleanup_interval_minutes", cleanup,
            "Recovers jobs left locked by crashed workers."),
]
