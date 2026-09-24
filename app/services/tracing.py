"""The correlation ID that ties one piece of work to every external call it makes.

    scheduler job / API request / worker
      → trace(...)  sets a trace ID and what the work is about
      → every external call underneath it (app.services.external_api) records that trace ID

A trace is ambient on purpose: services do not pass it around, and an integration added later inherits it for
free. Nothing here fails a caller — a missing trace only means a log row without a correlation ID.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

MAX_NAME = 60


@dataclass(frozen=True)
class TraceContext:
    """What the work running right now is: one correlation ID plus the entities it belongs to."""

    trace_id: str
    name: str | None = None  # the workflow that started it: hunter, ranking, tailor, api…
    workflow_type: str | None = None  # job | application | profile | system | agent
    workflow_entity_id: int | None = None
    job_id: int | None = None
    application_id: int | None = None
    user_id: int | None = None

    def public(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "workflow": self.name, "workflow_type": self.workflow_type,
                "workflow_entity_id": self.workflow_entity_id, "job_id": self.job_id,
                "application_id": self.application_id}


_current: ContextVar[TraceContext | None] = ContextVar("jobhunt_trace", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current() -> TraceContext | None:
    return _current.get()


def current_trace_id() -> str | None:
    ctx = _current.get()
    return ctx.trace_id if ctx else None


@contextmanager
def trace(name: str | None = None, *, trace_id: str | None = None, workflow_type: str | None = None,
          workflow_entity_id: int | None = None, job_id: int | None = None, application_id: int | None = None,
          user_id: int | None = None, inherit: bool = True):
    """Run a block under a correlation ID.

    Nested traces keep the outer ID by default, so "rank this job" inside a scheduler sweep stays one trace while
    still narrowing which job the calls belong to. Pass inherit=False to start a genuinely separate trace.
    """
    parent = _current.get() if inherit else None
    ctx = TraceContext(
        trace_id=trace_id or (parent.trace_id if parent else new_trace_id()),
        name=(name or (parent.name if parent else None) or "")[:MAX_NAME] or None,
        workflow_type=workflow_type or (parent.workflow_type if parent else None),
        workflow_entity_id=workflow_entity_id if workflow_entity_id is not None else (parent.workflow_entity_id if parent else None),
        job_id=job_id if job_id is not None else (parent.job_id if parent else None),
        application_id=application_id if application_id is not None else (parent.application_id if parent else None),
        user_id=user_id if user_id is not None else (parent.user_id if parent else None),
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def annotate(**fields: Any) -> None:
    """Add what has just become known (the job being worked on, say) to the trace already running."""
    ctx = _current.get()
    if ctx is None:
        return
    _current.set(replace(ctx, **{k: v for k, v in fields.items() if v is not None and hasattr(ctx, k)}))
