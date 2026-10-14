"""Running workers: every queued background task in one place, with the controls a person needs when a queue sticks.

    JD parsing   job_description_parse_tasks    scheduler job jd_parsing
    JD recovery  job_description_fetch_tasks    scheduler job jd_recovery
    Ranking      job_ranking_tasks              scheduler job ranking
    Job insights job_insight_tasks              scheduler job job_insights
    Tailoring    tailor_tasks (tailor → review)  the in-process tailor worker
    Reviewing    tailor_tasks at the review step  a view of Tailoring: the same rows, settings and controls

Two controls, both acting on the signed-in account's own tasks only:

* **Kill** stops a task's current attempt and requeues it fresh: attempts back to zero, retry wait cleared. A running
  thread cannot be interrupted, so killing takes its lease away instead: the old run's writes are guarded by that
  lease (ranker._owned, job_description._held, jd_recovery._held, tailor_tasks._write) and become no-ops, and the
  tailoring run stops at its next checkpoint, before saving anything. Killing every task of a kind also clears the
  account's provider rate-limit cooldowns (app.services.backoff.reset).
* **Run now** marks a task (`run_now_at`): the next claim takes it first, ignores its retry wait and the account's
  ranking cadence, and its calls skip the provider cooldown for that one run (app.services.backoff.bypass). A 429 is
  still recorded, so the task simply backs off again if the provider is still refusing.

Nothing here calls a model or a provider. Error text is redacted before it is returned.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobInsightTask,
    JobRankingTask,
    TailorTask,
)
from app.services import backoff, redaction, worker_settings
from app.services.lifecycle import STOPPED_BY_USER, WorkflowState
from app.settings import get_settings

RECENT = timedelta(hours=24)
ROW_LIMIT = 200
ERROR_LIMIT = 300
KILLED_NOTE = "Stopped by you and requeued"  # older rows; Kill now stops (lifecycle.STOPPED_BY_USER)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class WorkerError(Exception):
    """A control that cannot be applied, with the reason a person should see."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WorkerKind:
    """One queue of background tasks, described the same way for every worker so the screen stays generic."""

    kind: str
    label: str
    description: str
    model: Any
    scheduler_job: str | None  # None: the tailor worker polls on its own
    pending: str  # waiting status
    running: str
    finished: tuple[str, ...]
    max_attempts: Callable[[], int]
    node_id: str  # the AI Orchestration stage it feeds
    # A view of another worker's queue (Reviewing is the review step of Tailoring): the rows `scope` selects, that
    # worker's settings, and left out of totals and Kill all so no task is counted or stopped twice.
    scope: Callable[[Any], Any] | None = None
    view_of: str | None = None

    @property
    def active(self) -> tuple[str, str]:
        return (self.pending, self.running)

    @property
    def tailor(self) -> bool:
        return self.model is TailorTask

    @property
    def settings_kind(self) -> str:
        return self.view_of or self.kind

    @property
    def attempts(self) -> Any:
        return self.model.attempts if self.tailor else self.model.attempt_count

    def where(self, query: Any) -> Any:
        return query.where(self.scope(self.model)) if self.scope is not None else query


def _tailor_max() -> int:
    from app.services.tailor_tasks import MAX_ATTEMPTS

    return MAX_ATTEMPTS


def _reviewing(m: Any) -> Any:
    """The tailor tasks at the review step: reviewing now, a review-only run waiting, or finished while reviewing
    (tailor_tasks keeps a finished run's last stage)."""
    return or_(and_(m.status == "running", m.stage == "reviewing"), and_(m.status == "queued", m.mode == "skip"),
               and_(m.status.in_(("succeeded", "failed", "cancelled")), m.stage == "reviewing"))


KINDS: dict[str, WorkerKind] = {k.kind: k for k in (
    WorkerKind("parsing", "JD parsing", "Parses each posting into structured requirements (JD parser model).",
               JobDescriptionParseTask, "jd_parsing", "PENDING", "PROCESSING", ("COMPLETED", "FAILED", "SKIPPED"),
               lambda: get_settings().jd_parse_max_attempts, "jd_parser"),
    WorkerKind("recovery", "JD recovery", "Reads a description from the posting URL when the board sent none: HTTP, "
               "then the browser, a model only on ambiguous pages.", JobDescriptionFetchTask, "jd_recovery",
               "PENDING", "PROCESSING", ("COMPLETED", "FAILED", "BLOCKED", "SKIPPED"),
               lambda: get_settings().jd_fetch_max_attempts, "jd_recovery"),
    WorkerKind("ranking", "Ranking", "Scores each job's fit; code decides qualified or archived.", JobRankingTask,
               "ranking", "PENDING", "PROCESSING", ("COMPLETED", "FAILED", "SKIPPED"),
               lambda: get_settings().ranking_max_attempts, "ranker"),
    WorkerKind("insights", "Job insights", "Researches approved jobs (posting, company site, Glassdoor and PayScale "
               "when configured), then a model organises what was found.", JobInsightTask, "job_insights", "PENDING",
               "PROCESSING", ("COMPLETED", "PARTIAL", "FAILED", "SKIPPED"),
               lambda: get_settings().job_insights_max_attempts, "company_insights"),
    WorkerKind("tailoring", "Tailoring", "Tailors the resume and cover letter, then the reviewer checks the packet.",
               TailorTask, None, "queued", "running", ("succeeded", "failed", "cancelled"), _tailor_max, "tailor"),
    WorkerKind("reviewing", "Reviewing", "The reviewer checks each tailored packet and recommends PASS, RETAILOR or "
               "SKIP; code decides. It runs as the last step of a Tailoring task: Kill and Run now act on that task, "
               "and a retry tailors the job again.", TailorTask, None, "queued", "running",
               ("succeeded", "failed", "cancelled"), _tailor_max, "reviewer", scope=_reviewing, view_of="tailoring"),
)}


def kind_of(name: str) -> WorkerKind:
    spec = KINDS.get(name)
    if spec is None:
        raise WorkerError(f"Unknown worker '{name}'. Workers: {', '.join(KINDS)}", 404)
    return spec


def _lock(db: Session, query):
    return query.with_for_update() if db.get_bind().dialect.name == "postgresql" else query


# ───────────── reading ─────────────

def _display_status(spec: WorkerKind, task: Any, now: datetime) -> str:
    """RUNNING, QUEUED, RETRYING or the finished status, the same words for every worker."""
    status = task.status
    if status == spec.running:
        return "RUNNING"
    if status not in spec.active and (task.error if spec.tailor else task.last_error) == STOPPED_BY_USER:
        return "STOPPED"
    if status == spec.pending:
        waiting = getattr(task, "next_attempt_at", None)
        if (waiting is not None and waiting > now) or ((_attempts(spec, task) or 0) > 0 and not spec.tailor):
            return "RETRYING"
        return "QUEUED"
    return status.upper()


def _attempts(spec: WorkerKind, task: Any) -> int:
    return (task.attempts if spec.tailor else task.attempt_count) or 0


def row(spec: WorkerKind, task: Any, job: tuple[str, str] | None, now: datetime,
        max_attempts: int | None = None) -> dict[str, Any]:
    """One task as Running workers shows it. Metadata only. `max_attempts` is the account's own limit."""
    tailoring = spec.tailor
    status = _display_status(spec, task, now)
    error = task.error if tailoring else task.last_error
    category = None if tailoring else (task.error_category.value if task.error_category else None)
    finished_at = task.finished_at if tailoring else getattr(task, "completed_at", None)
    waiting = None if tailoring else task.next_attempt_at
    active = task.status in spec.active
    return {"task_id": task.id, "job_id": task.job_id, "title": job[0] if job else None,
            "company": job[1] if job else None, "kind": spec.kind, "status": status, "raw_status": task.status,
            "stage": (task.stage if active else "done") if tailoring else ("running" if status == "RUNNING" else status.lower()),
            "mode": task.mode if tailoring else None, "reason": getattr(task, "reason", None),
            "attempt": _attempts(spec, task), "max_attempts": max_attempts or spec.max_attempts(),
            "created_at": _iso(task.created_at), "started_at": _iso(task.started_at), "finished_at": _iso(finished_at),
            "next_attempt_at": _iso(waiting), "lease_expires_at": _iso(task.lock_expires_at),
            "last_error": redaction.redact_text(error, ERROR_LIMIT) if error and status != "COMPLETED" else None,
            "error_category": category, "run_now": task.run_now_at is not None,
            "result_code": getattr(task, "result_code", None),
            "can_kill": status in ("RUNNING", "RETRYING"),
            "can_run_now": status in ("QUEUED", "RETRYING") or status in STATUS_FILTERS["failed"],
            "retried_by": None,  # set by rows() when a newer task of the same job exists
            "can_remove": active and status != "RUNNING" and spec.kind in ("ranking", "tailoring"),
            **({"reviewer_score": (task.result or {}).get("reviewer_score")} if spec.kind == "reviewing" else {})}


# Status filters, the same for every worker: what a task is doing, in the words the screen uses.
STATUS_FILTERS = {
    "active": ("RUNNING", "QUEUED", "RETRYING"), "running": ("RUNNING",), "queued": ("QUEUED",),
    "retrying": ("RETRYING",), "failed": ("FAILED", "BLOCKED", "STOPPED"),
    "done": ("COMPLETED", "PARTIAL", "SUCCEEDED", "SKIPPED", "CANCELLED"),
}
SCAN_LIMIT = 2000  # rows read before filtering, so a match past the first page is still found


def bucket(status: str) -> str:
    """Which status filter a displayed status falls under (besides "active")."""
    return next(name for name, members in STATUS_FILTERS.items() if name != "active" and status in members)


# ───────────── clearing the finished list ─────────────
# "Clear finished" hides finished tasks from a tab's list; nothing is deleted. Each tab keeps the time it was cleared
# (ranking keeps its own, shared with the ranking tracker); finished tasks from before it stay out of the list, while
# metrics, the Performance card (performance), the activity chart and each job's history still count them.
CLEARED_PREFERENCES = "workers_cleared"  # AppSetting.preferences[...]: {kind: ISO time}


def cleared_at(db: Session, user_id: int, kind: str) -> datetime | None:
    spec = kind_of(kind)
    if spec.kind == "ranking":
        from app.services import ranker

        return ranker.finished_cleared_at(db, user_id)
    prefs = db.scalar(select(AppSetting.preferences).where(AppSetting.user_id == user_id)) or {}
    value = (prefs.get(CLEARED_PREFERENCES) or {}).get(spec.kind)
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def clear_finished(db: Session, user_id: int, kind: str, *, clear: bool = True,
                   now: datetime | None = None) -> dict[str, Any]:
    """Hide (or, with clear=False, show again) a tab's finished tasks. Only the view changes. Commits."""
    spec, now = kind_of(kind), now or utcnow()
    if spec.kind == "ranking":
        from app.services import ranker

        ranker.clear_finished(db, user_id, clear=clear, now=now)
    else:
        from sqlalchemy.orm.attributes import flag_modified

        from app.services import user_settings

        row = user_settings.lock_settings_row(db, user_settings.settings_for(db, user_id))
        prefs = dict(row.preferences or {})
        cleared = {k: v for k, v in (prefs.get(CLEARED_PREFERENCES) or {}).items() if k != spec.kind}
        if clear:
            cleared[spec.kind] = now.isoformat()
        prefs[CLEARED_PREFERENCES] = cleared
        row.preferences = prefs
        flag_modified(row, "preferences")
        db.commit()
    return {"kind": spec.kind, "cleared_at": _iso(cleared_at(db, user_id, spec.kind))}


def _hidden(db: Session, spec: WorkerKind, user_id: int, since: datetime | None, now: datetime) -> int:
    """How many finished tasks of the last day a clear is hiding."""
    if since is None:
        return 0
    m = spec.model
    finished_column = m.finished_at if spec.tailor else m.completed_at
    return db.scalar(spec.where(select(func.count()).select_from(m).where(
        m.user_id == user_id, m.status.not_in(spec.active), finished_column >= now - RECENT,
        finished_column <= since))) or 0


def rows(db: Session, user_id: int, kind: str, now: datetime | None = None, *, q: str = "",
         status: str = "") -> dict[str, Any]:
    """Active tasks (running first, then what runs next) and those finished in the last day, narrowed by a search
    (job title, company or `#id`) and a status filter. Bounded. `counts` are per status filter over the search, so the
    filter can say how many each choice would show."""
    spec, now = kind_of(kind), now or utcnow()
    if status and status not in STATUS_FILTERS:
        raise WorkerError(f"Unknown status filter '{status}'. Filters: {', '.join(STATUS_FILTERS)}", 422)
    m = spec.model
    finished_column = m.finished_at if spec.tailor else m.completed_at
    since = cleared_at(db, user_id, spec.kind)
    shown_from = max(now - RECENT, since) if since else now - RECENT
    query = spec.where(select(m).where(m.user_id == user_id, or_(m.status.in_(spec.active), finished_column > shown_from)
                                       if since else or_(m.status.in_(spec.active), finished_column >= shown_from)))
    text = (q or "").strip()
    if text.lstrip("#").isdigit():
        query = query.where(m.job_id == int(text.lstrip("#")))
    elif text:
        like = f"%{text.lower()[:100]}%"
        query = query.join(Job, Job.id == m.job_id).where(Job.user_id == user_id, or_(
            func.lower(Job.title).like(like), func.lower(Job.company).like(like)))
    tasks = list(db.scalars(query.order_by((m.status != spec.running), (m.status != spec.pending),
                                           m.run_now_at.is_(None), m.id).limit(SCAN_LIMIT)).all())
    labels = _labels(db, [t.job_id for t in tasks], user_id)
    limit = worker_settings.get(db, user_id, spec.settings_kind, "max_attempts")
    found = [row(spec, t, labels.get(t.job_id), now, limit) for t in tasks]
    _mark_retried(db, spec, user_id, found, now)
    counts = {name: sum(r["status"] in members for r in found) for name, members in STATUS_FILTERS.items()}
    counts["all"] = len(found)
    out = [r for r in found if not status or r["status"] in STATUS_FILTERS[status]]
    order = {"RUNNING": 0, "QUEUED": 1, "RETRYING": 2}
    out.sort(key=lambda r: (order.get(r["status"], 3), not r["run_now"],
                            r["next_attempt_at"] or "", -(r["task_id"]) if r["status"] not in order else r["task_id"]))
    attach_models(db, user_id, spec, out[:ROW_LIMIT])
    return {"rows": out[:ROW_LIMIT], "total": len(out), "truncated": len(out) > ROW_LIMIT or len(tasks) >= SCAN_LIMIT,
            "status_counts": counts, "filters": {"q": text, "status": status or "all"},
            "cleared_at": _iso(since), "cleared_hidden": _hidden(db, spec, user_id, since, now)}


ACTIVITY_HOURS = 24
ACTIVITY_LIMIT = 5000


def activity(db: Session, user_id: int, kind: str, now: datetime | None = None,
             hours: int = ACTIVITY_HOURS) -> dict[str, Any]:
    """The last day of one worker, hour by hour: tasks queued, finished done and finished failed, plus the typical run
    time and wait. Read from the task rows themselves; bounded."""
    spec, now = kind_of(kind), now or utcnow()
    m = spec.model
    finished_column = m.finished_at if spec.tailor else m.completed_at
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
    buckets = [{"hour": _iso(start + timedelta(hours=i)), "queued": 0, "done": 0, "failed": 0} for i in range(hours)]

    def slot(at: datetime | None) -> dict[str, Any] | None:
        if at is None or at < start:
            return None
        i = int((at - start).total_seconds() // 3600)
        return buckets[i] if 0 <= i < hours else None

    for (created,) in db.execute(spec.where(select(m.created_at).where(m.user_id == user_id, m.created_at >= start))
                                 .limit(ACTIVITY_LIMIT)).all():
        if (b := slot(created)) is not None:
            b["queued"] += 1
    runs, waits = [], []
    finished = db.execute(spec.where(select(m.status, m.created_at, m.started_at, finished_column)
                                     .where(m.user_id == user_id, finished_column >= start)).limit(ACTIVITY_LIMIT)).all()
    for status, created, started, ended in finished:
        b = slot(ended)
        if b is None:
            continue
        if status in spec.active:  # requeued since it finished
            continue
        b["failed" if status.upper() in STATUS_FILTERS["failed"] else "done"] += 1
        if started and ended and ended >= started:
            runs.append((ended - started).total_seconds())
        if created and started and started >= created:
            waits.append((started - created).total_seconds())
    done, failed = sum(b["done"] for b in buckets), sum(b["failed"] for b in buckets)
    return {"kind": spec.kind, "hours": buckets, "done": done, "failed": failed,
            "queued": sum(b["queued"] for b in buckets),
            "success_rate": round(done / (done + failed), 4) if done + failed else None,
            "median_run_seconds": _median(runs), "median_wait_seconds": _median(waits)}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return round(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2, 1)


# ───────────── performance over a range ─────────────
# The Performance card of the tabs that had none (JD parsing and JD recovery keep their own metrics). Read from the
# task rows and llm_request_logs of the selected range, never from the list: clearing the list changes nothing here.
PERFORMANCE_LIMIT = 5000
PERFORMANCE_STAGES = {"insights": ("COMPANY_INSIGHTS", "INTERVIEW_INSIGHTS")}  # else the orchestration node's stages
_DONE = ("COMPLETED", "PARTIAL", "SUCCEEDED")
_FAILED = ("FAILED", "BLOCKED")


def _llm_stages(spec: WorkerKind) -> tuple[str, ...]:
    from app.services import orchestration_topology

    found = orchestration_topology.node(spec.node_id)
    return PERFORMANCE_STAGES.get(spec.kind) or (found.llm_stages if found else ())


def llm_usage(db: Session, user_id: int, stages: Iterable[str], start: datetime, end: datetime) -> dict[str, Any]:
    """A worker's model calls in a range, the shape JD parsing's metrics use. Unknown cost and latency stay None."""
    from app.models import LLMRequestLog as L

    stages = tuple(stages)
    calls = db.execute(select(L.status, L.error_type, L.model, L.input_tokens, L.output_tokens, L.latency_ms,
                              L.estimated_cost).where(L.user_id == user_id, L.stage.in_(stages), L.created_at >= start,
                                                      L.created_at <= end).limit(PERFORMANCE_LIMIT)).all() if stages else []
    latencies = [c.latency_ms for c in calls if c.latency_ms is not None]
    costs = [c.estimated_cost for c in calls if c.estimated_cost is not None]
    by_model: dict[str, int] = {}
    errors: dict[str, int] = {}
    for c in calls:
        by_model[c.model or "unknown"] = by_model.get(c.model or "unknown", 0) + 1
        if c.status == "FAILED":
            errors[c.error_type or "OTHER"] = errors.get(c.error_type or "OTHER", 0) + 1
    return {"requests": len(calls), "failed": sum(1 for c in calls if c.status == "FAILED"),
            "input_tokens": sum(c.input_tokens or 0 for c in calls),
            "output_tokens": sum(c.output_tokens or 0 for c in calls),
            "estimated_cost": float(sum(costs)) if costs else None,
            "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "models": dict(sorted(by_model.items(), key=lambda kv: -kv[1])), "errors": errors,
            "stages": list(stages), "truncated": len(calls) >= PERFORMANCE_LIMIT}


def performance(db: Session, user_id: int, kind: str, start: datetime, end: datetime,
                now: datetime | None = None) -> dict[str, Any]:
    """One worker's results in a range: the tasks created in it, how they ended, how long they took, the errors they
    hit and its model calls, plus what that worker decides (ranking decisions, reviewer scores, partial research).
    Bounded and read only. The list's Clear does not touch any of it."""
    spec, now = kind_of(kind), now or utcnow()
    m, ended = spec.model, ("finished_at" if spec.tailor else "completed_at")
    tasks = list(db.scalars(spec.where(select(m).where(m.user_id == user_id, m.created_at >= start,
                                                       m.created_at <= end))
                            .order_by(m.id.desc()).limit(PERFORMANCE_LIMIT)).all())
    statuses = [_display_status(spec, t, now) for t in tasks]
    by_status: dict[str, int] = {}
    for s in statuses:
        by_status[s] = by_status.get(s, 0) + 1
    done = sum(by_status.get(s, 0) for s in _DONE)
    failed = sum(by_status.get(s, 0) for s in _FAILED)
    durations = [(getattr(t, ended) - t.started_at).total_seconds() * 1000
                 for t, s in zip(tasks, statuses, strict=True)
                 if s in _DONE and t.started_at and getattr(t, ended) and getattr(t, ended) >= t.started_at]
    waits = [(t.started_at - t.created_at).total_seconds() for t in tasks
             if t.started_at and t.created_at and t.started_at >= t.created_at]
    errors: dict[str, int] = {}
    categories: dict[str, int] = {}
    for t, s in zip(tasks, statuses, strict=True):
        if s not in _FAILED:
            continue
        text = t.error if spec.tailor else t.last_error
        key = (redaction.redact_text((text or "unknown").split("\n")[0], 160) or "unknown").strip()
        errors[key] = errors.get(key, 0) + 1
        if not spec.tailor:
            category = t.error_category.value if t.error_category else "UNKNOWN"
            categories[category] = categories.get(category, 0) + 1
    out: dict[str, Any] = {
        "kind": spec.kind, "start": _iso(start), "end": _iso(end),
        "tasks_created": len(tasks), "completed": done, "failed": failed,
        "stopped": by_status.get("STOPPED", 0),
        "skipped": by_status.get("SKIPPED", 0) + by_status.get("CANCELLED", 0),
        "active": sum(by_status.get(s, 0) for s in ("RUNNING", "QUEUED", "RETRYING")),
        "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "success_rate": round(done / (done + failed), 3) if done + failed else None,
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else None,
        "median_wait_seconds": _median(waits),
        "retried": sum(1 for t in tasks if _attempts(spec, t) > 1),
        "failures_by_category": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "top_errors": [{"error": e, "tasks": n} for e, n in sorted(errors.items(), key=lambda kv: -kv[1])[:5]],
        "llm": llm_usage(db, user_id, _llm_stages(spec), start, end),
        "truncated": len(tasks) >= PERFORMANCE_LIMIT,
    }
    if spec.kind == "ranking":
        from app.models import JobRanking

        ranked = db.execute(select(JobRanking.decision, JobRanking.overall_fit_score).where(
            JobRanking.user_id == user_id, JobRanking.created_at >= start, JobRanking.created_at <= end)
            .limit(PERFORMANCE_LIMIT)).all()
        decisions: dict[str, int] = {}
        for decision, _ in ranked:
            decisions[decision or "UNKNOWN"] = decisions.get(decision or "UNKNOWN", 0) + 1
        fits = [float(f) for _, f in ranked if f is not None]
        out["ranking"] = {"rankings": len(ranked), "decisions": dict(sorted(decisions.items(), key=lambda kv: -kv[1])),
                          "qualified": decisions.get("QUALIFIED", 0), "archived": decisions.get("ARCHIVED", 0),
                          "average_fit": round(sum(fits) / len(fits), 1) if fits else None,
                          "median_fit": _median(fits)}
    elif spec.kind == "insights":
        out["insights"] = {"complete": by_status.get("COMPLETED", 0), "partial": by_status.get("PARTIAL", 0)}
    elif spec.tailor:
        results = [t.result or {} for t, s in zip(tasks, statuses, strict=True) if s == "SUCCEEDED"]
        scores = [float(r["reviewer_score"]) for r in results if isinstance(r.get("reviewer_score"), int | float)]
        out["review"] = {"scored": len(scores), "median_score": _median(scores),
                         "lowest_score": min(scores) if scores else None,
                         "highest_score": max(scores) if scores else None,
                         "auto_approved": sum(1 for r in results if r.get("auto_approved")),
                         "untailored": sum(1 for r in results if r.get("untailored")),
                         "skip_mode": sum(1 for t in tasks if t.mode == "skip")}
    return out


def _newer(db: Session, spec: WorkerKind, user_id: int, job_ids: Iterable[int]) -> dict[int, Any]:
    """The latest task of each job: a failed task that has a newer one was already run again."""
    ids = list(set(job_ids))
    if not ids:
        return {}
    m = spec.model
    latest = db.execute(select(func.max(m.id)).where(m.user_id == user_id, m.job_id.in_(ids)).group_by(m.job_id)).scalars()
    return {t.job_id: t for t in db.scalars(select(m).where(m.id.in_(list(latest)))).all()}


def _mark_retried(db: Session, spec: WorkerKind, user_id: int, rows_: list[dict[str, Any]], now: datetime) -> None:
    """A failed or blocked task that was run again (a newer task exists for its job) can no longer be run now: the
    newer task is the one to watch. It says which task that is and what it is doing."""
    finished = [r for r in rows_ if r["status"] in STATUS_FILTERS["failed"]]
    newest = _newer(db, spec, user_id, (r["job_id"] for r in finished))
    for r in finished:
        latest = newest.get(r["job_id"])
        if latest is not None and latest.id != r["task_id"]:
            r["can_run_now"] = False
            r["retried_by"] = {"task_id": latest.id, "status": _display_status(spec, latest, now)}


# ───────────── the model a task uses ─────────────
# Read, never decided: the route and model come from the active configuration and the failover overlay
# (LLMClient.route_for), what a running task actually called comes from its own LLM request rows. No model is called.

def _route_label(route: str) -> str:
    if route == "LITELLM":
        return "LiteLLM"
    from app.services.user_settings import KEY_LABELS

    return KEY_LABELS.get(route.lower(), route.title())


def _via(plan: dict[str, Any]) -> str | None:
    if plan.get("mode") == "AUTO_ROUTER":
        return "auto router"
    if plan.get("mode") == "TIER" and plan.get("configured"):
        return f"tier {plan['configured']}"
    return None


def _change(plan: dict[str, Any], last: Any | None, finished: bool = False) -> dict[str, Any] | None:
    """What moved this task off its assigned model, newest fact first; None when nothing did. For a finished task only
    what happened in its own request counts: today's configuration or resting models say nothing about it."""
    if last is not None:
        if last.escalated:
            return {"kind": "ESCALATED", "from": plan.get("primary"), "to": last.model or last.requested_model,
                    "reason": (last.escalation_reason or "").replace("_", " ").lower() or "quality escalation"}
        if last.routing_cause == "FAILOVER":
            # the request's own fact; a row from before it was recorded falls back to today's plan only while active
            source = last.failed_over_from or (None if finished else plan.get("primary"))
            return {"kind": "FAILOVER", "from": source, "to": last.model or last.requested_model,
                    "reason": "its model was resting"}
        if last.fallback_used:
            return {"kind": "FALLBACK", "from": last.requested_model, "to": last.model,
                    "reason": "LiteLLM fell back after an error"}
        if finished:
            return None
        if plan.get("model") and last.requested_model and last.requested_model != plan["model"] \
                and last.requested_model != plan.get("failed_over_from"):
            return {"kind": "CONFIG", "from": last.requested_model, "to": plan["model"],
                    "reason": "the model was changed since its last request"}
    if plan.get("failed_over_from"):
        return {"kind": "RESTING", "from": plan["failed_over_from"], "to": plan.get("model"),
                "reason": "its model is resting, so a backup answers"}
    return None


def attach_models(db: Session, user_id: int, spec: WorkerKind | str, rows_: list[dict[str, Any]],
                  status_of: Callable[[dict[str, Any]], str | None] | None = None) -> None:
    """Add `model` (route, model, how it was chosen) and `model_change` to every row.

    A finished task shows the model of its own last request (between its creation and its finish), or `source:
    "none"` when it never called a model; only what happened in that request counts as a change (failover, LiteLLM
    fallback, escalation). A running or retrying task shows its own latest request when there is one; otherwise, like
    a waiting task, the model its next request goes to. Bounded (one query), metadata only, and it never raises.
    `status_of` reads a row's RUNNING / QUEUED / RETRYING when the rows are not Running workers rows (the ranking
    tracker, the tailoring task list); anything else is finished."""
    spec = kind_of(spec) if isinstance(spec, str) else spec
    status_of = status_of or (lambda r: r.get("status"))
    rows_ = [r for r in rows_ if r.get("job_id")]
    if not rows_:
        return
    try:
        from app.models import LLMRequestLog
        from app.services.llm import LLMClient
        from app.services.orchestration_topology import NODES

        workers = next((n.workers for n in NODES if n.id == spec.node_id), ())
        if not workers:
            return
        plan = LLMClient.for_user(db, user_id).route_for(workers[0])
        since = min((r["created_at"] for r in rows_ if r.get("created_at")), default=None)
        if since is None:
            return
        logs = db.execute(select(LLMRequestLog).where(
            LLMRequestLog.user_id == user_id, LLMRequestLog.workflow_type == "job",
            LLMRequestLog.workflow_entity_id.in_({r["job_id"] for r in rows_}),
            LLMRequestLog.worker_type.in_(workers), LLMRequestLog.created_at >= datetime.fromisoformat(since))
            .order_by(LLMRequestLog.id.desc()).limit(1000)).scalars().all()
        by_job: dict[int, list[Any]] = {}
        for log in logs:  # newest first
            by_job.setdefault(log.workflow_entity_id, []).append(log)
        for r in rows_:
            active = status_of(r) in STATUS_FILTERS["active"]
            start = datetime.fromisoformat(r["created_at"]) if r.get("created_at") else None
            finished = r.get("finished_at") or r.get("completed_at")
            end = datetime.fromisoformat(finished) + timedelta(seconds=5) if finished and not active else None
            last = next((log for log in by_job.get(r["job_id"], [])
                         if (start is None or log.created_at >= start) and (end is None or log.created_at <= end)),
                        None)
            if not active:
                r["model"] = _used(last, plan) if last is not None else {"model": None, "source": "none"}
                r["model_change"] = _change(plan, last, finished=True) if last is not None else None
                continue
            used = last is not None and status_of(r) in ("RUNNING", "RETRYING")
            r["model"] = _used(last, plan) if used else {
                "model": plan.get("model"), "route": plan["route"], "route_label": _route_label(plan["route"]),
                "via": _via(plan), "source": "next", "backups": plan.get("backups") or []}
            r["model_change"] = _change(plan, last if used else None)
    except Exception:
        import logging

        logging.getLogger("jobhunt.workers").warning("could not read task models", exc_info=True)


def _used(last: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """The model and route one of the task's own requests went to."""
    route = (last.provider or "").upper()
    route = "LITELLM" if route in ("LITELLM", "") else route
    return {"model": last.model or last.requested_model, "route": route, "route_label": _route_label(route),
            "via": _via(plan) if route == plan.get("route") else None, "source": "used",
            "backups": plan.get("backups") or []}


def ranking_status(item: dict[str, Any]) -> str | None:
    """A ranking tracker task in Running workers' words."""
    if item.get("status") == "PROCESSING":
        return "RUNNING"
    if item.get("status") == "PENDING":
        return "RETRYING" if item.get("attempt_count") else "QUEUED"
    return None


def attach_tailoring_models(db: Session, user_id: int, items: list[dict[str, Any]]) -> None:
    """Tailoring tasks: the reviewer's model while a task is at its review step, the tailor's otherwise."""
    status = {"running": "RUNNING", "queued": "QUEUED"}
    reviewing = [i for i in items if i.get("stage") == "reviewing"]
    attach_models(db, user_id, "reviewing", reviewing, lambda i: status.get(i.get("status")))
    attach_models(db, user_id, "tailoring", [i for i in items if i.get("stage") != "reviewing"],
                  lambda i: status.get(i.get("status")))


def _labels(db: Session, job_ids: list[int], user_id: int) -> dict[int, tuple[str, str]]:
    if not job_ids:
        return {}
    return {r[0]: (r[1], r[2]) for r in db.execute(select(Job.id, Job.title, Job.company)
                                                  .where(Job.id.in_(set(job_ids)), Job.user_id == user_id)).all()}


def counts(db: Session, user_id: int, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Running, queued and retrying tasks per worker: the tab badges and the sidebar count."""
    now = now or utcnow()
    out = {}
    for spec in KINDS.values():
        m = spec.model
        waiting = m.created_at if spec.tailor else m.next_attempt_at  # tailoring has no retry wait
        tasks = db.execute(spec.where(select(m.status, waiting, spec.attempts)
                                      .where(m.user_id == user_id, m.status.in_(spec.active))).limit(5000)).all()
        c = {"running": 0, "queued": 0, "retrying": 0}
        for status, wait_until, attempts in tasks:
            if status == spec.running:
                c["running"] += 1
            elif not spec.tailor and ((wait_until is not None and wait_until > now) or (attempts or 0) > 0):
                c["retrying"] += 1
            else:
                c["queued"] += 1
        c["active"] = c["running"] + c["queued"] + c["retrying"]
        out[spec.kind] = c
    return out


# ───────────── controls ─────────────

def _task(db: Session, spec: WorkerKind, user_id: int, task_id: int) -> Any:
    task = db.scalar(_lock(db, spec.where(select(spec.model).where(spec.model.id == task_id,
                                                                   spec.model.user_id == user_id))))
    if task is None:
        raise WorkerError("Task not found", 404)
    return task


def _requeue_fresh(db: Session, spec: WorkerKind, task: Any, user_id: int, now: datetime) -> bool:
    """Take the task away from whatever run holds it and put it back at the start of the queue. True if it ran."""
    was_running = task.status == spec.running
    if spec.tailor:
        task.status, task.stage, task.attempts = spec.pending, "queued", 0
        task.locked_by = task.lock_expires_at = task.started_at = None
        task.error = None
        return was_running
    task.status = spec.pending
    task.locked_by = task.lock_token = task.lock_expires_at = None
    if hasattr(task, "locked_at"):
        task.locked_at = None
    task.attempt_count, task.next_attempt_at = 0, None
    task.last_error, task.error_category = KILLED_NOTE, None
    if spec.kind == "ranking" and was_running:
        # The ranker moved the job to RANKING when it claimed the task; it waits for ranking again now.
        from app.services.work_queue import transition

        job = db.scalar(_lock(db, select(Job).where(Job.id == task.job_id, Job.user_id == user_id)))
        if job is not None and job.state is WorkflowState.RANKING:
            transition(db, job, WorkflowState.RANK_PENDING, actor=f"user:{user_id}",
                       reason="Stopped from Running workers; waiting to be ranked again", now=now)
    return was_running


def _stop(db: Session, spec: WorkerKind, task: Any, user_id: int, now: datetime) -> bool:
    """End a task as stopped by the person and keep its job out of the automatic queue. True if it was running.

    The lease goes with it, so a run still talking to a model can no longer write anything (the same guards as
    before); its job is not picked up again by any sweep until someone asks (Retry, Run now, or the job's own
    screen)."""
    stage = getattr(task, "stage", None)
    was_running = _requeue_fresh(db, spec, task, user_id, now)  # takes the lease away; ranking: job back to waiting
    if spec.tailor:
        from app.services.tailor_tasks import DECLINED

        # A run stopped while reviewing keeps that stage, so Reviewing still lists it.
        task.status, task.error, task.finished_at = "cancelled", STOPPED_BY_USER, now
        task.stage = "reviewing" if was_running and stage == "reviewing" else "done"
        job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
        if job is not None:
            job.auto_tailor_status = DECLINED  # automatic tailoring never queues it again on its own
        return was_running
    task.status = "FAILED" if spec.kind == "parsing" else "SKIPPED"
    task.completed_at, task.next_attempt_at = now, None
    task.last_error, task.error_category = STOPPED_BY_USER, None
    if spec.kind == "recovery":
        task.result_code = "STOPPED_BY_USER"
    if spec.kind == "ranking":
        from app.services.work_queue import transition

        job = db.scalar(_lock(db, select(Job).where(Job.id == task.job_id, Job.user_id == user_id)))
        if job is not None and job.state is WorkflowState.RANK_PENDING:
            # SKIPPED, like a job taken out of the ranking queue: no sweep queues it again (ranker.remove_from_ranking).
            transition(db, job, WorkflowState.SKIPPED, actor=f"user:{user_id}", reason=STOPPED_BY_USER, now=now)
    return was_running


def kill(db: Session, user_id: int, kind: str, task_ids: list[int] | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    """Stop running and retrying tasks and take them out of the queue. `task_ids` None: every one of this worker's
    tasks, and the account's provider cooldowns are cleared too. Commits."""
    spec, now = kind_of(kind), now or utcnow()
    m = spec.model
    query = spec.where(select(m).where(m.user_id == user_id, m.status.in_(spec.active)))
    if task_ids is not None:
        query = query.where(m.id.in_(task_ids))
    tasks = list(db.scalars(_lock(db, query.order_by(m.id))).all())
    if task_ids is not None and not tasks:
        raise WorkerError("That task has already finished", 409)
    running = stopped = 0
    stopped_jobs = []
    for task in tasks:
        if task.status != spec.running and _display_status(spec, task, now) != "RETRYING":
            continue  # a task still waiting its first turn is not stopped by Kill (Remove takes it out)
        running += _stop(db, spec, task, user_id, now)
        stopped += 1
        stopped_jobs.append(task.job_id)
    db.commit()
    if spec.kind == "parsing":
        from app.services import job_description

        for job_id in stopped_jobs:  # a stopped parse no longer holds ranking back: it reads the raw text, now
            job_description.hand_to_ranking(db, job_id)
    cleared = backoff.reset(db, user_id) if task_ids is None else []
    return {"kind": spec.kind, "stopped": stopped, "running": running, "cooldowns_cleared": cleared}


def kill_all(db: Session, user_id: int, now: datetime | None = None) -> dict[str, Any]:
    results = {kind: kill(db, user_id, kind, None, now) for kind, spec in KINDS.items() if spec.view_of is None}
    return {"workers": results, "stopped": sum(r["stopped"] for r in results.values()),
            "running": sum(r["running"] for r in results.values()),
            "cooldowns_cleared": sorted({p for r in results.values() for p in r["cooldowns_cleared"]})}


def _requeue_finished(db: Session, spec: WorkerKind, task: Any, user_id: int) -> Any:
    """A failed or blocked task gets a new one through the path that normally queues it."""
    job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
    if job is None:
        raise WorkerError("The job no longer exists", 404)
    if spec.kind == "ranking":
        from app.services import ranker

        out = ranker.request_ranking(db, [job.id], force=True, actor=f"user:{user_id}")
        if out["skipped"]:
            raise WorkerError(out["skipped"][0]["reason"])
    elif spec.kind == "parsing":
        from app.services import job_description

        if job_description.queue_parse(db, job, reason="user_request") is None:
            raise WorkerError("This job has no description to parse")
        db.commit()
    elif spec.kind == "recovery":
        from app.services import jd_recovery

        if jd_recovery.enqueue(db, job, reason="user_request", force=True) is None:
            raise WorkerError("This job has no posting URL to recover from")
        db.commit()
    elif spec.kind == "insights":
        from app.services.job_insights import tasks as insight_tasks

        insight_tasks.enqueue(db, job, reason=insight_tasks.USER_REQUEST, force=True)
        db.commit()
    else:
        from app.services import tailor_tasks

        # Regenerate: a run that failed in review may already have saved its packet, which a plain enqueue refuses.
        out = tailor_tasks.enqueue(db, [job.id], task.mode, True)
        if out["skipped"]:
            raise WorkerError(f"Cannot tailor again: {out['skipped'][0]['reason']}")
    fresh = db.scalar(select(spec.model).where(spec.model.job_id == job.id, spec.model.user_id == user_id,
                                               spec.model.status.in_(spec.active)))
    if fresh is None:
        raise WorkerError("The job could not be queued again")
    return fresh


RETRY_LIMIT = 500


def retry(db: Session, user_id: int, kind: str, task_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    """Queue many failed or blocked tasks again at once, each through its worker's normal path (_requeue_finished).

    Per task, never all-or-nothing: a task that is not the account's, has not failed, or whose job was already run
    again is skipped with its reason. Unlike Run now nothing jumps the queue or skips a provider cooldown: failures
    in bulk usually mean an outage, and the retried tasks wait their turn like any other. Commits."""
    spec, now = kind_of(kind), now or utcnow()
    ids = list(dict.fromkeys(int(i) for i in task_ids))[:RETRY_LIMIT]
    if not ids:
        raise WorkerError("Choose at least one task to retry", 422)
    tasks = {t.id: t for t in db.scalars(spec.where(select(spec.model).where(spec.model.user_id == user_id,
                                                                             spec.model.id.in_(ids)))).all()}
    retried, skipped = [], []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None:
            skipped.append({"task_id": task_id, "reason": "Not found"})
            continue
        shown = _display_status(spec, task, now)
        if shown not in STATUS_FILTERS["failed"]:
            skipped.append({"task_id": task_id, "reason": f"Not failed ({shown.lower()})"})
            continue
        latest = _newer(db, spec, user_id, [task.job_id]).get(task.job_id)
        if latest is not None and latest.id != task.id:
            skipped.append({"task_id": task_id, "reason": f"Already run again as task #{latest.id}"})
            continue
        try:
            fresh = _requeue_finished(db, spec, task, user_id)
            db.commit()
        except WorkerError as exc:
            db.rollback()
            skipped.append({"task_id": task_id, "reason": str(exc)})
            continue
        retried.append({"task_id": task_id, "new_task_id": fresh.id, "job_id": task.job_id})
    return {"kind": spec.kind, "retried": retried, "skipped": skipped}


def run_now(db: Session, user_id: int, kind: str, task_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Put one task at the front of its queue, past its retry wait, the ranking cadence and provider cooldowns.
    A failed task is queued again first. Commits; the caller nudges the scheduler (run_soon)."""
    spec, now = kind_of(kind), now or utcnow()
    task = _task(db, spec, user_id, task_id)
    if task.status == spec.running:
        raise WorkerError("This task is running right now")
    if task.status not in spec.active:
        if _display_status(spec, task, now) not in STATUS_FILTERS["failed"]:
            raise WorkerError("This task already finished; queue the job again from its screen")
        latest = _newer(db, spec, user_id, [task.job_id]).get(task.job_id)
        if latest is not None and latest.id != task.id:
            raise WorkerError(f"Already run again as task #{latest.id} "
                              f"({_display_status(spec, latest, now).lower()}); use Run now on that one")
        task = _requeue_finished(db, spec, task, user_id)
    task.run_now_at = now
    if not spec.tailor:
        task.next_attempt_at = None
    if spec.kind == "ranking":
        task.force = True  # a ranking the person asked for: the cadence never holds it back
    db.commit()
    labels = _labels(db, [task.job_id], user_id)
    return row(spec, task, labels.get(task.job_id), now, worker_settings.get(db, user_id, spec.settings_kind, "max_attempts"))


def run_queued_now(db: Session, user_id: int, kind: str, now: datetime | None = None) -> dict[str, Any]:
    """Rank (or parse, recover, research) everything that is waiting, now: every queued or waiting task of the account
    loses its retry wait, and ranking tasks become the person's own request, so the account's cadence, a paused queue
    or auto-ranking being off no longer hold them back.

    Unlike one task's Run now this does not set run_now_at: a provider cooldown still applies, because many tasks at
    once are exactly what makes a provider refuse (the same rule as bulk Retry). Tasks a person stopped stay stopped;
    tasks whose description is still being read wait for it (the ranking claim skips them). Commits; the caller wakes
    the worker (run_soon)."""
    spec, now = kind_of(kind), now or utcnow()
    if spec.tailor:
        raise WorkerError("Tailoring starts as soon as a slot is free: there is no wait to skip")
    tasks = db.scalars(select(spec.model).where(spec.model.user_id == user_id, spec.model.status == spec.pending,
                                                or_(spec.model.last_error.is_(None), spec.model.last_error != STOPPED_BY_USER))
                       .order_by(spec.model.id).limit(RETRY_LIMIT)).all()
    for task in tasks:
        task.next_attempt_at = None
        if spec.kind == "ranking":
            task.force = True
    db.commit()
    return {"kind": spec.kind, "released": len(tasks), "task_ids": [t.id for t in tasks]}


# ───────────── the screen ─────────────

def scheduler_facts(scheduler: Any, job: str | None) -> dict[str, Any] | None:
    """When this worker's scheduler job runs. Schedule facts only: results and errors span every account."""
    if job is None:
        settings = get_settings()
        return {"name": "tailor_worker", "enabled": bool(settings.tailor_worker_enabled), "interval_minutes": None,
                "poll_seconds": 2, "concurrency": settings.tailor_concurrency, "running": None, "next_run_at": None,
                "last_finished_at": None, "last_status": None}
    if scheduler is None:
        return None
    found = next((j for j in scheduler.status()["jobs"] if j["name"] == job), None)
    if found is None:
        return None
    return {k: found.get(k) for k in ("name", "enabled", "interval_minutes", "running", "next_run_at",
                                      "last_finished_at", "last_status")}


def summary(db: Session, user_id: int, scheduler: Any = None, now: datetime | None = None) -> dict[str, Any]:
    from app.services import worker_handoff

    now = now or utcnow()
    per_kind = counts(db, user_id, now)
    return {"generated_at": _iso(now), "cooldowns": backoff.active(db, user_id, now=now),
            "workers": [{"kind": s.kind, "label": s.label, "description": s.description, "node_id": s.node_id,
                         "counts": per_kind[s.kind], "scheduler": scheduler_facts(scheduler, s.scheduler_job),
                         "paused": not worker_settings.get(db, user_id, s.settings_kind, "auto"), "view_of": s.view_of,
                         "cleared_at": _iso(cleared_at(db, user_id, s.kind)),
                         # Nudge forward: where this worker's finished jobs go next, and how many are stuck before it.
                         "next_stage": worker_handoff.next_stage(db, user_id, s.kind)}
                        for s in KINDS.values()],
            "active": sum(per_kind[k]["active"] for k, s in KINDS.items() if s.view_of is None),
            "running": sum(per_kind[k]["running"] for k, s in KINDS.items() if s.view_of is None)}
