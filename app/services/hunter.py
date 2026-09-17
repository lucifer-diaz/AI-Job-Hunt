"""Apify Hunter: searches start actor runs and return at once; results are imported later by the sync.

    SearchProfile / POST /api/hunter/search
      → plan_runs (one adapter per actor)
      → start each actor run on its own (a run that fails to start does not stop the others)
      → SearchRun rows track PENDING / RUNNING / SUCCEEDED / FAILED / NEEDS_INVESTIGATION
    apify_sync (scheduler) → refresh_hunter_run
      → fetch dataset → schema check → normalize → recency filter → deduplicate → save
      → new jobs DISCOVERED → NORMALIZED → hard filters (app.services.ranker) → FILTERED → RANK_PENDING
        with a ranking task. Nothing is ranked here: the scheduler's ranking worker does that later.

Every step is safe to repeat: a re-imported dataset merges into the jobs it already created.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, SearchProfile, SearchRun
from app.services import ranker, sources
from app.services.apify_adapters import (FALLBACK_FOR_SITE, PRIMARY, HunterSearch, PlannedRun, RunContext,
                                         SchemaMismatch, get_adapter, older_than, plan_runs)
from app.services.lifecycle import WorkflowState as S
from app.services.user_settings import apify_token
from app.services.work_queue import transition
from app.settings import get_settings

log = logging.getLogger("jobhunt.hunter")

PENDING, RUNNING, SUCCEEDED, FAILED = "PENDING", "RUNNING", "SUCCEEDED", "FAILED"
NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"
HUNTER_STATUSES = (PENDING, RUNNING, SUCCEEDED, FAILED, NEEDS_INVESTIGATION)
NOT_STARTED_PREFIX = "not-started-"
MAX_DISCOVERIES = 50
FILLABLE = ("direct_apply_url", "canonical_apply_url", "scraped_at", "job_type", "remote_type", "seniority", "skills",
            "experience", "salary_min", "salary_max", "salary_currency", "salary_interval", "salary_source")


class HunterConfigError(RuntimeError):
    """The account cannot run searches yet, e.g. no Apify token saved."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _hunter_status(apify_status: str) -> str:
    if apify_status == "SUCCEEDED":
        return SUCCEEDED
    if apify_status in ("FAILED", "ABORTED", "TIMED-OUT"):
        return FAILED
    return RUNNING if apify_status in ("RUNNING", "ABORTING", "TIMING-OUT") else PENDING


def _apify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return (f"Apify did not answer within {get_settings().apify_request_timeout_seconds}s, "
                "so the run may not have started")
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = ((exc.response.json() or {}).get("error") or {}).get("message")
        except ValueError:
            detail = None
        code = exc.response.status_code
        prefix = "Apify rejected the actor input" if code == 400 else f"Apify returned HTTP {code}"
        return f"{prefix}: {detail or exc.response.text[:300]}"
    if isinstance(exc, httpx.HTTPError):
        return f"Could not reach Apify: {type(exc).__name__}"
    return f"{type(exc).__name__}: {exc}"


# ───────────── starting runs ─────────────

def start_search(db: Session, owner: int, search: HunterSearch, *, search_profile_id: int | None = None,
                 fallback_for_run_id: int | None = None) -> dict[str, Any]:
    """Start every actor run a search needs and return without waiting for any of them."""
    token = apify_token(db, owner)
    if not token:
        raise HunterConfigError(sources.MISSING_APIFY_TOKEN)
    plans, warnings = plan_runs(search)
    limit = get_settings().hunter_max_runs_per_search
    if len(plans) > limit:
        raise ValueError(f"This search needs {len(plans)} actor runs; the limit is {limit}. "
                         "Use fewer roles, locations or sites.")
    runs = [_start_run(db, owner, token, plan, search, search_profile_id, fallback_for_run_id) for plan in plans]
    return {"runs": runs, "warnings": warnings}


def _start_run(db: Session, owner: int, token: str, plan: PlannedRun, search: HunterSearch,
               search_profile_id: int | None, fallback_for_run_id: int | None) -> SearchRun:
    now = utcnow()
    run = SearchRun(user_id=owner, provider="apify", actor_id=plan.actor_id, adapter=plan.adapter, input_json=plan.input,
                    roles=plan.roles, location=plan.location, sites=plan.sites, search_params=search.params(),
                    search_profile_id=search_profile_id, fallback_for_run_id=fallback_for_run_id, created_at=now)
    try:
        path = f"/acts/{sources._apify_actor_api_id(plan.actor_id)}/runs"
        data = (sources.apify_post(path, plan.input, token=token) or {}).get("data") or {}
        if not data.get("id"):
            raise RuntimeError("Apify did not return a run ID")
    except Exception as exc:  # one run failing to start must not stop the others
        run.external_run_id = f"{NOT_STARTED_PREFIX}{uuid.uuid4().hex}"
        run.status, run.hunter_status = "FAILED", FAILED
        run.error = _apify_error(exc)[:2000]
        run.finished_at = run.completed_at = now
        log.warning("Apify run for %s (%s) did not start: %s", plan.actor_id, plan.location, run.error)
    else:
        run.external_run_id = str(data["id"])
        run.status = str(data.get("status") or "READY")
        run.hunter_status = _hunter_status(run.status)
        run.dataset_id = str(data.get("defaultDatasetId") or "")
        run.started_at = sources._apify_time(data.get("startedAt")) or now
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# ───────────── syncing runs ─────────────

def _run_failure(status: str, data: dict[str, Any]) -> str:
    message = str(data.get("statusMessage") or data.get("errorMessage") or "").strip()
    reason = {"TIMED-OUT": "Apify run timed out before it finished",
              "ABORTED": "Apify run was aborted"}.get(status, "Apify actor run failed")
    return f"{reason}{': ' + message if message else ''}. Results it collected were imported."[:2000]


def _dataset_items(run: SearchRun, token: str, status: str) -> list[Any]:
    if not run.dataset_id:
        if status == "SUCCEEDED":
            raise SchemaMismatch(["the run succeeded but returned no default dataset"])
        return []
    try:
        payload = sources.apify_get(f"/datasets/{run.dataset_id}/items",
                                    {"format": "json", "clean": "true", "limit": get_settings().apify_dataset_limit},
                                    token=token)
    except httpx.HTTPError:
        if status == "SUCCEEDED":
            raise  # try again on the next sync
        return []  # a failed run may have no dataset
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    if not isinstance(payload, list):
        raise SchemaMismatch([f"the dataset response is a {type(payload).__name__}, not a list of items"])
    return payload


def refresh_hunter_run(db: Session, run: SearchRun, reimport: bool = False) -> dict[str, Any]:
    """Check one run on Apify; once it finished, import its dataset. Safe to call repeatedly."""
    if run.external_run_id.startswith(NOT_STARTED_PREFIX):
        return run_summary(run)
    token = apify_token(db, run.user_id) if run.user_id is not None else None
    if not token:
        raise RuntimeError("The account that started this search has no Apify API token")
    data = (sources.apify_get(f"/actor-runs/{run.external_run_id}", token=token) or {}).get("data") or {}
    status = str(data.get("status") or run.status)
    run.status = status
    run.dataset_id = str(data.get("defaultDatasetId") or run.dataset_id or "")
    run.started_at = run.started_at or sources._apify_time(data.get("startedAt"))
    if status not in sources.APIFY_TERMINAL_STATES:
        run.hunter_status = _hunter_status(status)
        db.commit()
        return run_summary(run)
    if run.completed_at is not None and not reimport:
        db.commit()
        return run_summary(run)
    run.finished_at = sources._apify_time(data.get("finishedAt")) or utcnow()
    db.commit()  # the import commits job by job; keep the run's own state first

    try:
        stats = import_run_items(db, run, _dataset_items(run, token, status))
    except SchemaMismatch as exc:
        stats = {"seen": 0, "created": 0, "schema_problems": exc.problems}
        run.hunter_status = NEEDS_INVESTIGATION
        run.error = f"Actor output did not match the {get_adapter(run.adapter).name} adapter: {exc}"[:2000]
        log.error("Apify run %s (%s) needs investigation: %s", run.external_run_id, run.actor_id, exc)
    else:
        run.hunter_status = SUCCEEDED if status == "SUCCEEDED" else FAILED
        run.error = "" if status == "SUCCEEDED" else _run_failure(status, data)
        if status != "SUCCEEDED":
            log.warning("Apify run %s (%s) finished as %s", run.external_run_id, run.actor_id, status)
    run.item_count = int(stats.get("seen", 0))
    run.created_jobs = int(stats.get("created", 0))
    run.stats = stats
    run.completed_at = run.completed_at or utcnow()
    db.commit()
    if run.hunter_status in (FAILED, NEEDS_INVESTIGATION):
        start_fallback(db, run)
    return run_summary(run)


def start_fallback(db: Session, run: SearchRun) -> list[int]:
    """When a multi-board run fails, search its boards once more with their dedicated actors."""
    if not get_settings().apify_auto_fallback or run.adapter != PRIMARY or run.fallback_for_run_id is not None:
        return []
    sites = [site for site in run.sites or [] if site in FALLBACK_FOR_SITE]
    already = db.scalar(select(SearchRun.id).where(SearchRun.fallback_for_run_id == run.id).limit(1))
    if not sites or already or run.user_id is None:
        return []
    search = HunterSearch.from_dict({**(run.search_params or {}), "roles": run.roles, "locations": [run.location],
                                     "sites": sites, "mode": "fallback"})
    try:
        started = start_search(db, run.user_id, search, search_profile_id=run.search_profile_id,
                               fallback_for_run_id=run.id)["runs"]
    except Exception as exc:
        log.warning("Fallback searches for Apify run %s did not start: %s", run.external_run_id, exc)
        run.stats = {**(run.stats or {}), "fallback_error": str(exc)[:300]}
        db.commit()
        return []
    run.stats = {**(run.stats or {}), "fallback_run_ids": [r.id for r in started]}
    db.commit()
    return [r.id for r in started]


# ───────────── importing results ─────────────

def import_run_items(db: Session, run: SearchRun, items: list[Any]) -> dict[str, Any]:
    """Normalize, filter, deduplicate and save one run's dataset. Raises SchemaMismatch before writing anything."""
    adapter = get_adapter(run.adapter)
    problems = adapter.check_schema(items)
    if problems:
        raise SchemaMismatch(problems)
    params = run.search_params or {}
    reference = run.finished_at or run.started_at or run.created_at
    ctx = RunContext(roles=tuple(run.roles or ()), location=run.location or "", actor_id=run.actor_id,
                     run_id=run.external_run_id, reference_time=reference, search_profile_id=run.search_profile_id)
    policy = ranker.policy_for(db, run.user_id)
    stats: Counter = Counter()
    for item in items:
        if not isinstance(item, dict):
            stats["invalid_items"] += 1
            continue
        job = adapter.normalize_result(item, ctx)
        if isinstance(job, str):
            stats[job] += 1
            continue
        stats["normalized"] += 1
        if job["posted_at"] is None:
            stats["missing_posted_date"] += 1
            if params.get("require_posted_date"):
                stats["dropped_without_posted_date"] += 1
                continue
        elif adapter.recency != "exact" and older_than(job["posted_at"], job["posted_precision"],
                                                       job["scraped_at"] or reference, params.get("hours_old")):
            stats["too_old"] += 1
            continue
        stats[_save(db, run, ctx, job, policy)] += 1
    summary = {"seen": len(items), **{key: 0 for key in ("normalized", "created", "duplicates", "filtered_out",
                                                          "missing_title", "missing_company", "missing_posted_date",
                                                          "too_old")}}
    summary.update(stats)
    summary["created"] += summary.pop("created_filtered_out", 0)
    summary["empty"] = not items
    return summary


def _discovery(run: SearchRun, ctx: RunContext, job: dict[str, Any]) -> dict[str, Any]:
    return {"role": job["matched_search_role"], "location": ctx.location or None, "board": job["source_board"],
            "actor_id": run.actor_id, "apify_run_id": run.external_run_id, "search_run_id": run.id,
            "search_profile_id": run.search_profile_id, "discovered_at": utcnow().isoformat()}


def find_duplicate(db: Session, owner: int, job: dict[str, Any]) -> Job | None:
    """The saved job this result duplicates: by board job ID, then canonical URL, then company + title + city."""
    mine = select(Job).where(Job.user_id == owner).order_by(Job.id).limit(1)
    board, native = job["source_board"], job["source_job_id"]
    if native:
        legacy_refs = [f"{board}:{native}"] + ([f"{board}:{job['raw']['id']}"] if job["raw"].get("id") else [])
        found = db.scalars(mine.where(or_(and_(Job.source_board == board, Job.source_job_id == native),
                                          and_(Job.source == f"apify:{board}", Job.source_ref.in_(legacy_refs))))).first()
        if found:
            return found
    urls = [u for u in (job["canonical_url"], job["canonical_apply_url"]) if u]
    if urls:
        found = db.scalars(mine.where(or_(Job.canonical_url.in_(urls), Job.canonical_apply_url.in_(urls)))).first()
        if found:
            return found
    return db.scalars(mine.where(Job.dedupe_key == job["dedupe_key"])).first()


def _merge(existing: Job, run: SearchRun, ctx: RunContext, job: dict[str, Any]) -> None:
    """Record another way the job was found and fill in details the first discovery lacked."""
    entry = _discovery(run, ctx, job)
    signature = lambda e: (e.get("role"), e.get("location"), e.get("board"), e.get("apify_run_id"))  # noqa: E731
    entries = list(existing.discoveries or [])
    if len(entries) < MAX_DISCOVERIES and all(signature(e) != signature(entry) for e in entries):
        existing.discoveries = [*entries, entry]
    if existing.posted_at is None and job["posted_at"] is not None:
        existing.posted_at, existing.posted_date_raw = job["posted_at"], job["posted_date_raw"]
    for field in FILLABLE:
        if getattr(existing, field) in (None, "", []) and job[field] not in (None, "", []):
            setattr(existing, field, job[field])
    if existing.source_job_id is None and existing.source_board in (None, job["source_board"]) and job["source_job_id"]:
        existing.source_board, existing.source_job_id = job["source_board"], job["source_job_id"]
    if existing.canonical_url is None and job["canonical_url"]:
        existing.canonical_url = job["canonical_url"]
    if len(job["description"]) > len(existing.description or ""):
        existing.description = job["description"]


def _new_job(run: SearchRun, ctx: RunContext, job: dict[str, Any]) -> Job:
    board = job["source_board"]
    basis = job["source_job_id"] or job["canonical_url"] or job["canonical_apply_url"] or job["dedupe_key"]
    ref = job["source_job_id"] or hashlib.sha256(basis.encode()).hexdigest()[:40]
    return Job(
        user_id=run.user_id, source=f"apify:{board}", source_ref=f"{board}:{ref}"[:500], state=S.DISCOVERED,
        title=job["title"], company=job["company"], location=job["location"],
        url=job["direct_apply_url"] or job["job_url"] or "", description=job["description"], raw=job["raw"],
        status="discovered", discovered_at=utcnow(), search_run_id=run.id,
        source_board=board, source_job_id=job["source_job_id"], source_url=job["job_url"],
        direct_apply_url=job["direct_apply_url"], canonical_url=job["canonical_url"],
        canonical_apply_url=job["canonical_apply_url"], dedupe_key=job["dedupe_key"],
        posted_at=job["posted_at"], posted_date_raw=job["posted_date_raw"], scraped_at=job["scraped_at"],
        job_type=job["job_type"], remote_type=job["remote_type"], seniority=job["seniority"], skills=job["skills"],
        experience=job["experience"], salary_min=job["salary_min"], salary_max=job["salary_max"],
        salary_currency=job["salary_currency"], salary_interval=job["salary_interval"],
        salary_source=job["salary_source"], matched_search_role=job["matched_search_role"],
        matched_search_location=ctx.location or None, apify_actor_id=run.actor_id, apify_run_id=run.external_run_id,
        search_profile_id=run.search_profile_id, discoveries=[_discovery(run, ctx, job)],
    )


def _save(db: Session, run: SearchRun, ctx: RunContext, job: dict[str, Any], policy: ranker.RankingPolicy) -> str:
    """Save one normalized result and commit. Returns duplicates, created or created_filtered_out.

    A new job passes the ranker's hard filters and is queued for ranking in the same transaction; the model is
    never called from an import.
    """
    existing = find_duplicate(db, run.user_id, job)
    if existing is not None:
        _merge(existing, run, ctx, job)
        db.commit()
        return "duplicates"
    record = _new_job(run, ctx, job)
    try:
        db.add(record)
        db.flush()
        transition(db, record, S.NORMALIZED, actor="hunter",
                   reason=f"Normalized from {run.actor_id} run {run.external_run_id}")
        reason = ranker.queue_job(db, record, policy=policy, actor="hunter")
        db.commit()
    except IntegrityError:  # another sync saved the same posting a moment ago
        db.rollback()
        existing = find_duplicate(db, run.user_id, job)
        if existing is None:
            raise
        _merge(existing, run, ctx, job)
        db.commit()
        return "duplicates"
    return "created_filtered_out" if reason else "created"


# ───────────── search profiles ─────────────

def profile_search(profile: SearchProfile) -> HunterSearch:
    return HunterSearch.from_dict({"roles": profile.roles, "locations": profile.locations, "sites": profile.sites,
                                   "hours_old": profile.hours_old, "max_results": profile.max_results,
                                   "remote_only": profile.remote_only, "job_type": profile.job_type,
                                   "country": profile.country, "mode": profile.mode,
                                   "require_posted_date": profile.require_posted_date})


def run_profile(db: Session, profile: SearchProfile, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    profile.last_run_at = now
    profile.next_run_at = now + timedelta(minutes=profile.run_every_minutes)
    db.commit()
    return start_search(db, profile.user_id, profile_search(profile), search_profile_id=profile.id)


def run_due_profiles(db: Session, owner: int, now: datetime) -> dict[str, list]:
    """Start the account's enabled profiles that are due. The guarded UPDATE makes each start happen once."""
    due = or_(SearchProfile.next_run_at.is_(None), SearchProfile.next_run_at <= now)
    profiles = db.scalars(select(SearchProfile).where(SearchProfile.user_id == owner, SearchProfile.enabled.is_(True), due)
                          .order_by(SearchProfile.id)).all()
    run_ids, errors = [], []
    for profile in profiles:
        claimed = db.execute(update(SearchProfile).where(SearchProfile.id == profile.id,
                                                         SearchProfile.enabled.is_(True), due)
                             .values(last_run_at=now, next_run_at=now + timedelta(minutes=profile.run_every_minutes))
                             .execution_options(synchronize_session=False)).rowcount
        db.commit()
        if not claimed:
            continue  # another instance started it
        try:
            runs = start_search(db, owner, profile_search(profile), search_profile_id=profile.id)["runs"]
        except Exception as exc:  # one profile failing must not stop the others
            db.rollback()
            errors.append({"search_profile_id": profile.id, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        run_ids += [run.id for run in runs]
    return {"run_ids": run_ids, "errors": errors}


def sync_pending_runs(db: Session, owner: int, limit: int = 50) -> dict[str, Any]:
    runs = db.scalars(select(SearchRun).where(SearchRun.user_id == owner, SearchRun.adapter.is_not(None),
                                              SearchRun.completed_at.is_(None)).order_by(SearchRun.id).limit(limit)).all()
    summary: dict[str, Any] = {"checked": 0, "completed": 0, "created": 0, "errors": []}
    for run in runs:
        try:
            out = refresh_hunter_run(db, run)
        except Exception as exc:
            db.rollback()
            summary["errors"].append({"id": run.id, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        summary["checked"] += 1
        summary["completed"] += int(out["completed"])
        summary["created"] += (out["import"] or {}).get("created", 0)
    return summary


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def run_summary(run: SearchRun) -> dict[str, Any]:
    started = not run.external_run_id.startswith(NOT_STARTED_PREFIX)
    return {"id": run.id, "status": run.hunter_status, "apify_status": run.status, "adapter": run.adapter,
            "actor_id": run.actor_id, "run_id": run.external_run_id if started else None,
            "search_profile_id": run.search_profile_id, "fallback_for_run_id": run.fallback_for_run_id,
            "roles": run.roles or [], "location": run.location, "sites": run.sites or [], "input": run.input_json,
            "dataset_id": run.dataset_id or None, "created_at": _iso(run.created_at), "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at), "result_count": run.item_count, "created_jobs": run.created_jobs,
            "error": run.error or None, "completed": run.completed_at is not None, "import": run.stats}
