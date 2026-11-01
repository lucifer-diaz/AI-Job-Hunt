"""Automatic tailoring: when ranking qualifies a job, queue it for tailoring (Running workers → Settings → Tailoring).

Deterministic, no model. This module only writes queued `tailor_tasks` rows through `tailor_tasks.enqueue`; the
tailor worker claims them within the account's tailoring concurrency, so a task starts at once when a slot is
free and otherwise waits in the queue until one is.

A job is queued automatically only when all of these hold:
* the account turned automatic tailoring on, and tailoring runs on Job Hunt OS workers (not Claude Desktop);
* the job is QUALIFIED and still in the Tailor queue (`shortlisted`);
* it meets the account's automatic-tailoring fit score, when one is set;
* it was ranked after automatic tailoring was turned on — jobs that were already waiting are never backfilled;
* automatic tailoring has not decided about it yet (`jobs.auto_tailor_status` is NULL) and it has no tailoring
  task. The decision is kept on the job, not the task: QUEUED once this module queued it, DECLINED once the person
  cancelled one of its tasks. Clearing finished tasks therefore never lets a cancelled or failed job back in.

The one other job it queues is a packet whose resume the person changed (`discovery.repoint_jobs`: re-tailoring,
`auto_tailor_status` RESUME_CHANGED): once the new ranking qualifies it, with no tailoring task still active. The fit
score and the other account rules apply as above.

Each job is claimed with a guarded UPDATE before it is queued, and `uq_tailor_tasks_active_job` allows one active
task per job, so the ranking hook, the sweep and a person clicking Tailor at the same moment queue it once.

`after_qualified` runs straight after a ranking; `sweep` is the scheduler's safety net for a job the hook missed.
Neither ever changes the ranking's outcome: every failure is caught and logged.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import Job, JobRanking, TailorTask
from app.services import preparation, system_log, tailor_tasks
from app.services.lifecycle import WorkflowState as S
from app.services.user_settings import auto_tailor_for, execution_mode_for
from app.services.workflow import NEEDS_RETAILOR

log = logging.getLogger("jobhunt.auto_tailor")

SessionFactory = Callable[[], Session]
TAILOR_QUEUE_STATUS = "shortlisted"
# A job automatic tailoring may claim: undecided, or a packet whose resume the person changed.
CLAIMABLE = or_(Job.auto_tailor_status.is_(None), Job.auto_tailor_status == tailor_tasks.RESUME_CHANGED)


def _enabled_at(config: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(config["enabled_at"]) if config.get("enabled_at") else None
    except (TypeError, ValueError):
        return None


def _candidates(db: Session, config: dict[str, Any], *, manual: bool = False):
    """The query for jobs automatic tailoring may queue, or None when it is off for this account.

    `manual` is a person pressing Nudge forward on Running workers: they ask for the jobs that qualified and never
    reached tailoring, so the "ranked after automatic tailoring was turned on" rule is dropped. Automatic tailoring
    must still be on (it is the account's choice to spend model calls on tailoring without being asked each time), and
    everything else (qualified, in the Tailor queue, the fit score, no task or decision yet) still applies."""
    since = _enabled_at(config)
    if not config["enabled"] or (since is None and not manual) or execution_mode_for(db) != "backend":
        return None
    first = and_(Job.status == TAILOR_QUEUE_STATUS, Job.auto_tailor_status.is_(None),
                 ~exists().where(TailorTask.job_id == Job.id))
    retailor = and_(Job.status == NEEDS_RETAILOR, Job.auto_tailor_status == tailor_tasks.RESUME_CHANGED,
                    ~exists().where(TailorTask.job_id == Job.id, TailorTask.status.in_(tailor_tasks.ACTIVE)))
    query = select(Job).where(Job.state == S.QUALIFIED, Job.fit_score.is_not(None), or_(first, retailor),
                              preparation.untouched())
    if not manual:
        ranked_at = (select(func.max(JobRanking.created_at)).where(JobRanking.job_id == Job.id)
                     .correlate(Job).scalar_subquery())
        query = query.where(ranked_at >= since)
    if config["min_fit_score"] is not None:
        query = query.where(Job.fit_score >= config["min_fit_score"])
    return query


def queue(db: Session, job_ids: list[int] | None = None, *, manual: bool = False) -> dict[str, Any]:
    """Queue the eligible jobs among `job_ids` (every eligible job when None). Commits."""
    query = _candidates(db, auto_tailor_for(db), manual=manual)
    if query is None:
        return {"queued": [], "skipped": []}
    if job_ids is not None:
        query = query.where(Job.id.in_(job_ids))
    ids = list(db.scalars(query.with_only_columns(Job.id).order_by(Job.fit_score.desc(), Job.id)).all())
    # Claim each job before queueing it. The ranking hook and the sweep can pick the same job at the same moment; the
    # guarded UPDATE lets exactly one of them win (the other waits on the row and then matches nothing). The claim
    # commits together with the task in enqueue, so a failure undoes both and the sweep can try again.
    claimed = [job_id for job_id in ids if db.execute(
        update(Job).where(Job.id == job_id, CLAIMABLE)
        .values(auto_tailor_status=tailor_tasks.AUTO_QUEUED).execution_options(synchronize_session=False)).rowcount]
    if not claimed:
        db.rollback()
        return {"queued": [], "skipped": []}
    result = _hand_to_graph(db, claimed) if preparation.routes(db) else tailor_tasks.enqueue(db, claimed)
    for task in result["queued"]:
        system_log.log_event(message=f"Automatic tailoring queued job {task['job_id']} after ranking",
                             category="WORKFLOW", workflow="auto_tailor", user_id=db.info.get("user_id"),
                             entity_type="job", entity_id=task["job_id"], status="QUEUED")
    return result


def _hand_to_graph(db: Session, job_ids: list[int]) -> dict[str, Any]:
    """PREPARATION_GRAPH_MODE=on: each claimed job gets a preparation graph run, which queues its tailoring pass
    itself. Commits (with the claim, as enqueue does)."""
    queued, skipped = [], []
    for job in db.scalars(select(Job).where(Job.id.in_(job_ids))).all():
        task = preparation.dispatch(db, job, reason="auto")
        if task is None:
            skipped.append({"id": job.id, "reason": "held until you ask again"})
        else:
            queued.append({"job_id": job.id, "preparation_task_id": task.id})
    db.commit()
    return {"queued": queued, "skipped": skipped}


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


def waiting_job_ids(db: Session, limit: int = 200) -> list[int]:
    """Qualified jobs that never reached tailoring (for Running workers → Nudge forward), best fit first."""
    query = _candidates(db, auto_tailor_for(db), manual=True)
    if query is None:
        return []
    return list(db.scalars(query.with_only_columns(Job.id).order_by(Job.fit_score.desc(), Job.id).limit(limit)).all())


def sweep(db: Session) -> dict[str, int] | None:
    """Queue every eligible job of the session's account. None when automatic tailoring is off for it."""
    if _candidates(db, auto_tailor_for(db)) is None:
        return None
    result = queue(db)
    return {"queued": len(result["queued"]), "skipped": len(result["skipped"])}
