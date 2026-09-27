"""Postings that no longer exist are deleted, and stay deleted.

    recovery or the listing check reads the posting URL → 404/410 or "no longer accepting applications"
      → remove_posting(): tombstone (removed_postings) + a Monitoring → Logs line + delete the job
      → the next search that returns the same posting skips it (is_removed)

Jobs the person approved or applied to are never deleted: the log says the posting closed and the job stays. Only a
page that says the posting is gone counts — a login wall, a challenge, a timeout or a server error never deletes
anything. With AUTO_DELETE_REMOVED_JOBS=false removed postings are only marked (the recovery task says so).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
    RemovedPosting,
)
from app.services import system_log
from app.settings import get_settings

# Jobs the person has committed to: never deleted automatically.
PROTECTED_STATUSES = ("approved", "applied")
REMOVED_REASONS = ("JOB_REMOVED", "JOB_CLOSED")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def protected(db: Session, job: Job) -> bool:
    if job.status in PROTECTED_STATUSES:
        return True
    return db.scalar(select(Application.id).where(Application.job_id == job.id,
                                                  Application.applied_at.is_not(None))) is not None


def is_removed(db: Session, owner: int, *, source: str | None = None, source_ref: str | None = None,
               canonical_url: str | None = None, dedupe_key: str | None = None) -> bool:
    """Whether this posting was deleted earlier because it no longer existed."""
    matches = []
    if source and source_ref:
        matches.append((RemovedPosting.source == source) & (RemovedPosting.source_ref == source_ref))
    if canonical_url:
        matches.append(RemovedPosting.canonical_url == canonical_url)
    if dedupe_key:
        matches.append(RemovedPosting.dedupe_key == dedupe_key)
    if not matches:
        return False
    return db.scalar(select(RemovedPosting.id).where(RemovedPosting.user_id == owner, or_(*matches)).limit(1)) \
        is not None


def remove_posting(db: Session, job: Job, reason: str, *, actor: str, now: datetime | None = None) -> str:
    """Delete a job whose posting is gone, leaving a tombstone. Returns deleted, kept (protected) or disabled.
    Does not commit."""
    now = now or utcnow()
    details = {"job_id": job.id, "title": job.title, "company": job.company, "url": job.url, "reason": reason,
               "actor": actor}
    if not get_settings().auto_delete_removed_jobs:
        return "disabled"
    if protected(db, job):
        system_log.log_event(category="WORKFLOW", level="WARNING", operation="posting_removed", workflow="DISCOVERY",
                             user_id=job.user_id, entity_type="job", entity_id=job.id, status="kept",
                             message=f"Posting for {job.title} at {job.company} is gone; kept because you approved "
                                     "or applied to it", details=details)
        return "kept"
    db.add(RemovedPosting(user_id=job.user_id, source=job.source or "", source_ref=job.source_ref or "",
                          canonical_url=job.canonical_url, dedupe_key=job.dedupe_key, title=(job.title or "")[:300],
                          company=(job.company or "")[:300], reason=reason[:40], removed_at=now))
    system_log.log_event(category="WORKFLOW", level="INFO", operation="posting_removed", workflow="DISCOVERY",
                         user_id=job.user_id, entity_type="job", entity_id=job.id, status="deleted",
                         message=f"Deleted {job.title} at {job.company}: the posting no longer exists",
                         details=details)
    delete_job(db, job)
    return "deleted"


def delete_job(db: Session, job: Job) -> None:
    """Delete a job and its task rows. PostgreSQL cascades these; deleting them here too keeps SQLite installs free of
    orphans (a reused job id would otherwise inherit another job's tasks). Does not commit."""
    for model in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask):
        for row in db.scalars(select(model).where(model.job_id == job.id)).all():
            db.delete(row)
    db.delete(job)


# ───────────── the listing check ─────────────

def due_jobs(db: Session, now: datetime, limit: int) -> list[tuple[int, int]]:
    """(job id, owner) of postings worth re-checking: a URL, not applied or approved, not checked recently."""
    from app.services.lifecycle import WorkflowState as S

    cutoff = now - timedelta(days=max(0, get_settings().listing_check_max_age_days))
    live = (S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING, S.RANKED, S.QUALIFIED, S.SKIPPED, S.ARCHIVED)
    return list(db.execute(
        select(Job.id, Job.user_id).where(Job.user_id.is_not(None), Job.url != "", Job.state.in_(live),
                                          Job.status.not_in(PROTECTED_STATUSES),
                                          or_(Job.listing_checked_at.is_(None), Job.listing_checked_at < cutoff))
        .order_by(Job.listing_checked_at.asc().nulls_first(), Job.id).limit(max(1, limit))).all())


def check_one(sessions: Callable[[], Session], job_id: int, owner: int, now: datetime) -> str:
    """Read one posting over plain HTTP. Only a removed or closed page deletes; anything else just records the
    check. Never the browser, never a model."""
    from app.services import jd_recovery

    with sessions() as db:
        db.info["user_id"] = owner
        job = db.get(Job, job_id)
        if job is None:
            return "missing"
        snapshot = jd_recovery.Snapshot(job.id, owner, job.url, job.title or "", job.company or "", 1)
    try:
        result = jd_recovery._http_stage(snapshot)
    except Exception:  # noqa: BLE001  one posting failing never stops the check
        result = None
    with sessions() as db:
        db.info["user_id"] = owner
        job = db.get(Job, job_id)
        if job is None:
            return "missing"
        job.listing_checked_at = now
        outcome = "live"
        if result is not None and result.kind == "removed":
            outcome = remove_posting(db, job, result.code or "JOB_REMOVED", actor="listing_check", now=now)
        elif result is None or result.kind in ("blocked", "transient", "permanent", "not_html"):
            outcome = "unknown"  # never deleted on a guess
        db.commit()
        return outcome


async def check_listings(sessions: Callable[[], Session], now: datetime, *, limit: int | None = None,
                         concurrency: int | None = None) -> dict[str, Any]:
    with sessions() as db:
        due = due_jobs(db, now, limit or get_settings().listing_check_batch_size)
    semaphore = asyncio.Semaphore(max(1, concurrency or get_settings().jd_fetch_max_concurrency))

    async def run(job_id: int, owner: int) -> str:
        async with semaphore:
            return await asyncio.to_thread(check_one, sessions, job_id, owner, now)

    outcomes = await asyncio.gather(*(run(job_id, owner) for job_id, owner in due))
    summary = {"checked": len(due)}
    for outcome in outcomes:
        summary[outcome] = summary.get(outcome, 0) + 1
    return summary
