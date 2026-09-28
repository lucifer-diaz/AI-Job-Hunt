"""Automatic tailoring: when ranking qualifies a job, queue it for tailoring (Settings → Workflow & cadence).

Deterministic, no model. This module only writes queued `tailor_tasks` rows through `tailor_tasks.enqueue`; the
tailor worker claims them within the account's tailoring concurrency, so a task starts at once when a slot is
free and otherwise waits in the queue until one is.

A job is queued automatically only when all of these hold:
* the account turned automatic tailoring on, and tailoring runs on Job Hunt OS workers (not Claude Desktop);
* the job is QUALIFIED and still in the Tailor queue (`shortlisted`);
* it meets the account's automatic-tailoring fit score, when one is set;
* it was ranked after automatic tailoring was turned on — jobs that were already waiting are never backfilled;
* it never had a tailoring task, so a task the person cancelled is never queued again.

`after_qualified` runs straight after a ranking; `sweep` is the scheduler's safety net for a job the hook missed.
Neither ever changes the ranking's outcome: every failure is caught and logged.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import Job, JobRanking, TailorTask
from app.services import system_log, tailor_tasks
from app.services.lifecycle import WorkflowState as S
from app.services.user_settings import auto_tailor_for, execution_mode_for

log = logging.getLogger("jobhunt.auto_tailor")

SessionFactory = Callable[[], Session]
TAILOR_QUEUE_STATUS = "shortlisted"


def _enabled_at(config: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(config["enabled_at"]) if config.get("enabled_at") else None
    except (TypeError, ValueError):
        return None


def _candidates(db: Session, config: dict[str, Any]):
    """The query for jobs automatic tailoring may queue, or None when it is off for this account."""
    since = _enabled_at(config)
    if not config["enabled"] or since is None or execution_mode_for(db) != "backend":
        return None
    ranked_at = (select(func.max(JobRanking.created_at)).where(JobRanking.job_id == Job.id)
                 .correlate(Job).scalar_subquery())
    query = select(Job).where(Job.state == S.QUALIFIED, Job.status == TAILOR_QUEUE_STATUS,
                              Job.fit_score.is_not(None), ranked_at >= since,
                              ~exists().where(TailorTask.job_id == Job.id))
    if config["min_fit_score"] is not None:
        query = query.where(Job.fit_score >= config["min_fit_score"])
    return query


def queue(db: Session, job_ids: list[int] | None = None) -> dict[str, Any]:
    """Queue the eligible jobs among `job_ids` (every eligible job when None). Commits."""
    query = _candidates(db, auto_tailor_for(db))
    if query is None:
        return {"queued": [], "skipped": []}
    if job_ids is not None:
        query = query.where(Job.id.in_(job_ids))
    ids = list(db.scalars(query.with_only_columns(Job.id).order_by(Job.fit_score.desc(), Job.id)).all())
    if not ids:
        return {"queued": [], "skipped": []}
    result = tailor_tasks.enqueue(db, ids)
    for task in result["queued"]:
        system_log.log_event(message=f"Automatic tailoring queued job {task['job_id']} after ranking",
                             category="WORKFLOW", workflow="auto_tailor", user_id=db.info.get("user_id"),
                             entity_type="job", entity_id=task["job_id"], status="QUEUED")
    return result


def after_qualified(sessions: SessionFactory, user_id: int, job_id: int) -> bool:
    """Queue one job the ranker just qualified, when the account wants that. Never raises."""
    try:
        with sessions() as db:
            db.info["user_id"] = user_id
            return bool(queue(db, [job_id])["queued"])
    except Exception as exc:  # the ranking is saved; the sweep tries again later
        log.warning("automatic tailoring could not queue job %s", job_id, exc_info=True)
        system_log.log_event(message=f"Automatic tailoring could not queue job {job_id}: {type(exc).__name__}: {exc}",
                             category="WORKFLOW", level="WARNING", workflow="auto_tailor", user_id=user_id,
                             entity_type="job", entity_id=job_id, status="FAILED")
        return False


def sweep(db: Session) -> dict[str, int] | None:
    """Queue every eligible job of the session's account. None when automatic tailoring is off for it."""
    if _candidates(db, auto_tailor_for(db)) is None:
        return None
    result = queue(db)
    return {"queued": len(result["queued"]), "skipped": len(result["skipped"])}
