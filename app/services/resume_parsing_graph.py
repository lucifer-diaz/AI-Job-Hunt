"""Running one resume structuring task through its LangGraph graph (app.agents.resume_parsing), or without it.

    RESUME_PARSE_GRAPH_MODE=on   (the default) each claimed task runs as a graph thread (a thread per task attempt),
                                 on the same task row, claim and lease as the plain path
    RESUME_PARSE_GRAPH_MODE=off  resume_parsing.process: the same steps in sequence

The graph needs AGENT_GRAPHS_ENABLED and PostgreSQL; without them the plain path runs. Kill, Run now and Retry act on
the task row as before: Kill clears the claim's token and stops the thread, so no node writes again.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.services import resume_parsing
from app.services.resume_parsing import ParseClaim
from app.settings import get_settings

log = logging.getLogger("jobhunt.resume_parsing")
SessionFactory = Callable[[], Session]


def _usable(sessions: SessionFactory) -> bool:
    if get_settings().resume_parse_graph_mode_value != "on":
        return False
    with sessions() as db:
        return db.get_bind().dialect.name == "postgresql"


def run(sessions: SessionFactory, claim_: ParseClaim, *, saver: Any | None = None,
        hold: Callable[[str], None] | None = None, graph: bool | None = None) -> str:
    """Structure one claimed task by the configured path. Returns an outcome (resume_parsing.OUTCOMES); never raises.
    `graph` forces the choice (tests run the graph on SQLite with an in-memory saver)."""
    if graph if graph is not None else _usable(sessions):
        return process(sessions, claim_, saver=saver, hold=hold)
    return resume_parsing.process(sessions, claim_)


def process(sessions: SessionFactory, claim_: ParseClaim, *, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Start (or continue) the task attempt's graph thread and run it to its end. Never raises."""
    from app.agents import resume_parsing as graph
    from app.agents import runtime
    from app.services import tracing

    try:
        with sessions() as db:
            db.info["user_id"] = claim_.user_id
            task = resume_parsing.held(db, claim_)
            if task is None:
                return "lost"
            # One thread per task attempt (a retry is a new attempt and a new run); the same key finds the same one.
            key = f"resume_parsing:{task.resume_id}:{task.id}:{task.created_at.isoformat()}:a{task.attempt_count}"
            thread = runtime.start(db, graph.GRAPH, {"resume_id": task.resume_id, "task_id": task.id,
                                                     "attempt": task.attempt_count}, idempotency_key=key)
            task = resume_parsing.held(db, claim_)
            if task is None:
                return "lost"
            task.agent_thread_id = thread.id
            db.commit()
            thread_pk = thread.id
            run_ = runtime.claim(db, thread_pk, claim_.user_id, ("QUEUED", "RUNNING"), claim_.worker_id)
        if run_ is None:
            return _finished(sessions, claim_, thread_pk)
        with tracing.trace("resume_parsing", user_id=claim_.user_id):
            runtime.execute(sessions, run_, None, saver=saver, hold=hold,
                            extra={"task_id": claim_.task_id, "claim": claim_})
        return _finished(sessions, claim_, thread_pk)
    except Exception as exc:
        log.warning("resume parsing graph task %s failed", claim_.task_id, exc_info=True)
        return resume_parsing.fail(sessions, claim_, f"{type(exc).__name__}: {exc}")


def _finished(sessions: SessionFactory, claim_: ParseClaim, thread_pk: int) -> str:
    """The outcome of a thread that ended: the one its last step settled, or a failure settled the plain way."""
    from app.models import AgentThread

    with sessions() as db:
        db.info["user_id"] = claim_.user_id
        thread = db.get(AgentThread, thread_pk)
        status = thread.status if thread is not None else "STOPPED"
        result = dict(thread.result or {}) if thread is not None else {}
        error = (thread.last_error or "") if thread is not None else ""
    if status == "COMPLETED":
        outcome = str(result.get("outcome") or "")
        return outcome if outcome in resume_parsing.OUTCOMES else "lost"
    if status == "FAILED":
        return resume_parsing.fail(sessions, claim_, error or "graph run failed")
    return "lost"  # stopped, or still RUNNING under another worker's lease: that worker settles it
