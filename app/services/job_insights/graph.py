"""Running one research task through the Job Insights graph (app.agents.job_insights), or the current path.

    JOB_INSIGHTS_GRAPH_MODE=on      each claimed research task runs as a graph thread (a thread per task attempt),
                                    on the same task row, claim and lease as the current path
    JOB_INSIGHTS_GRAPH_MODE=shadow  the current path researches (service.process); after it, the graph's
                                    deterministic steps re-check the run read-only (compensation recomputed, the
                                    provenance check over the saved sections) and a difference is logged
    JOB_INSIGHTS_GRAPH_MODE=off     (the default) service.process, exactly as before

The graph needs AGENT_GRAPHS_ENABLED and PostgreSQL; without them the current path runs. Kill, Run now and Retry act on
the task row as before: Kill clears the claim's token, so the graph's next guarded write finds nothing to hold.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.services import system_log
from app.services.job_insights import service
from app.services.job_insights.tasks import InsightClaim
from app.settings import get_settings

log = logging.getLogger("jobhunt.job_insights")
SessionFactory = Callable[[], Session]


def mode() -> str:
    return get_settings().job_insights_graph_mode_value


def _usable(sessions: SessionFactory) -> bool:
    if not get_settings().agent_graphs_enabled:
        return False
    with sessions() as db:
        return db.get_bind().dialect.name == "postgresql"


def run(sessions: SessionFactory, claim_: InsightClaim, *, saver: Any | None = None,
        hold: Callable[[str], None] | None = None) -> str:
    """Research one claimed task by the configured path. Returns an outcome (service.OUTCOMES); never raises."""
    current = mode()
    if service.scope_of(sessions, claim_) == service.application_form.FORM_ONLY:
        return service.process(sessions, claim_)  # form drafts only: no research for the graph to conduct
    if current == "on" and _usable(sessions):
        return process(sessions, claim_, saver=saver, hold=hold)
    label = service.process(sessions, claim_)
    if current == "shadow" and label in ("completed", "partial") and _usable(sessions):
        shadow(sessions, claim_.user_id, claim_.job_id, claim_.task_id)
    return label


def process(sessions: SessionFactory, claim_: InsightClaim, *, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Start (or continue) the task attempt's graph thread and run it to its end. Never raises."""
    from app.agents import job_insights as graph
    from app.agents import runtime
    from app.services import tracing
    from app.services.job_insights import tasks

    started = time.monotonic()
    try:
        with sessions() as db:
            db.info["user_id"] = claim_.user_id
            task = tasks.held(db, claim_)
            if task is None:
                return "lost"
            # One thread per task attempt (a retry is a new attempt and a new run); the same key finds the same one.
            key = f"job_insights:{task.job_id}:{task.id}:{task.created_at.isoformat()}:a{task.attempt_count}"
            thread = runtime.start(db, graph.GRAPH, {"job_id": task.job_id, "task_id": task.id,
                                                     "attempt": task.attempt_count, "mode": "ON"},
                                   idempotency_key=key)
            task = tasks.held(db, claim_)
            if task is None:
                return "lost"
            task.agent_thread_id = thread.id
            db.commit()
            thread_pk = thread.id
            run_ = runtime.claim(db, thread_pk, claim_.user_id, ("QUEUED", "RUNNING"), claim_.worker_id)
        if run_ is None:
            return _finished(sessions, claim_, thread_pk)
        with tracing.trace("job_insights", job_id=claim_.job_id, user_id=claim_.user_id):
            runtime.execute(sessions, run_, None, saver=saver, hold=hold,
                            extra={"task_id": claim_.task_id, "claim": claim_})
        label = _finished(sessions, claim_, thread_pk)
    except Exception as exc:
        log.warning("job insights graph task %s failed", claim_.task_id, exc_info=True)
        label = service._crashed(sessions, claim_, type(exc).__name__)
    _log(claim_, label, int((time.monotonic() - started) * 1000))
    return label


def _finished(sessions: SessionFactory, claim_: InsightClaim, thread_pk: int) -> str:
    """The outcome of a thread that ended: the label PERSIST_INSIGHTS returned, or a crash settled the current way."""
    from app.models import AgentThread

    with sessions() as db:
        db.info["user_id"] = claim_.user_id
        thread = db.get(AgentThread, thread_pk)
        status = thread.status if thread is not None else "STOPPED"
        result = dict(thread.result or {}) if thread is not None else {}
        error = (thread.last_error or "") if thread is not None else ""
    if status == "COMPLETED":
        outcome = str(result.get("outcome") or "")
        return outcome if outcome in service.OUTCOMES else "completed"
    if status == "STOPPED":
        return "lost"
    if status == "FAILED":
        return service._crashed(sessions, claim_, (error.split(":", 1)[0] or "graph run failed")[:80])
    return "lost"  # still RUNNING under another worker's lease: that worker settles it


def shadow(sessions: SessionFactory, user_id: int, job_id: int, task_id: int) -> dict[str, Any] | None:
    """The graph's deterministic steps over what the current path just saved, read-only, on an in-memory checkpoint.
    Logs a difference; never writes to the research tables and never calls a source or a model."""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agents import effects, tracing_boundary
    from app.agents import job_insights as graph

    try:
        app = graph.build(InMemorySaver())
        context = graph.InsightsContext(run=effects.RunHandle(0, user_id, "shadow"), sessions=sessions,
                                        task_id=task_id)
        # Like every graph run (runtime.execute): inside the export boundary, so LANGSMITH_TRACING in the environment
        # can never add an unpoliced tracer to this one.
        with tracing_boundary.export(graph.EXPORT_FIELDS) as callbacks:
            state = app.invoke({"job_id": job_id, "task_id": task_id, "mode": "SHADOW"},
                               config={"configurable": {"thread_id": f"shadow:{user_id}:{task_id}"},
                                       "callbacks": callbacks, "run_name": "agent:job_insights:shadow"},
                               context=context)
    except Exception:
        log.warning("job insights shadow check for task %s failed", task_id, exc_info=True)
        return None
    result = graph.summary(dict(state))
    if result.get("code") == "DIFFERS":
        system_log.log_event(category="WORKFLOW", level="WARNING", operation="job_insights_shadow",
                             workflow="job_insights", entity_type="job", entity_id=job_id, user_id=user_id,
                             status="differs", message="Job insights graph (shadow) differs from the current path",
                             details={k: result.get(k) for k in ("task_id", "comp_status", "stored_comp_status",
                                                                  "provenance_violations")})
    return result


def _log(claim_: InsightClaim, label: str, latency: int) -> None:
    level = "INFO" if label in ("completed", "partial") else "WARNING" if label in ("retrying", "skipped", "lost") \
        else "ERROR"
    system_log.log_event(category="WORKFLOW", level=level, operation="job_insights", workflow=service.WORKER,
                         user_id=claim_.user_id, entity_type="job", entity_id=claim_.job_id, status=label,
                         duration_ms=latency, message=f"Job insights {label} (graph)",
                         details={"worker": service.WORKER, "job_id": claim_.job_id, "task_id": claim_.task_id,
                                  "graph": "job_insights"})
