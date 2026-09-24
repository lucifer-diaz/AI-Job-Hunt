"""Operational events worth keeping: what Monitoring → Logs shows.

Written from the edges of the system — the instrumentation layer when an external call fails or crawls, the
scheduler when a job finishes, workflows at their milestones — and never from ordinary business code.

Two rules, both of them the same rule: this is best effort, and it is safe. A failure to write a log line is
swallowed, and every message and detail goes through app.services.redaction first, so a credential that reaches
this module still never reaches the database.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.services import tracing
from app.services.redaction import redact_text, redact_value

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.system_log")

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
CATEGORIES = ("EXTERNAL_API", "LLM", "SCHEDULER", "WORKFLOW", "APIFY", "APPLICATION", "EMAIL", "SYSTEM")
MAX_MESSAGE = 500

_sessions: Callable[[], Session] | None = None


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def use_sessions(factory: Callable[[], Session] | None) -> None:
    global _sessions
    _sessions = factory


def _session_factory() -> Callable[[], Session]:
    if _sessions is not None:
        return _sessions
    from app.db import SessionLocal

    return SessionLocal


def log_event(*, message: str, category: str = "SYSTEM", level: str = "INFO", provider: str | None = None,
              operation: str | None = None, workflow: str | None = None, trace_id: str | None = None,
              user_id: int | None = None, entity_type: str | None = None, entity_id: int | None = None,
              status: str | None = None, duration_ms: int | None = None, external_request_id: str | None = None,
              details: dict[str, Any] | None = None) -> None:
    """Record one operational event. Never raises."""
    try:
        from app.models import SystemLogEntry
        from app.settings import get_settings

        if get_settings().system_log_retention_days <= 0:
            return
        ctx = tracing.current()
        entry = SystemLogEntry(
            created_at=utcnow(),
            level=level if level in LEVELS else "INFO",
            category=category if category in CATEGORIES else "SYSTEM",
            provider=(provider or "")[:30] or None, operation=(operation or "")[:60] or None,
            workflow=(workflow or (ctx.name if ctx else None) or "")[:60] or None,
            trace_id=trace_id or (ctx.trace_id if ctx else None),
            user_id=user_id if user_id is not None else (ctx.user_id if ctx else None),
            entity_type=(entity_type or "")[:30] or None, entity_id=entity_id,
            message=redact_text(message, MAX_MESSAGE) or "",
            status=(status or "")[:30] or None, duration_ms=duration_ms,
            external_request_id=external_request_id,
            details=redact_value(details) if details else None)
        with _session_factory()() as db:
            db.add(entry)
            db.commit()
    except Exception:  # noqa: BLE001  logging is best effort by design
        log.warning("could not record system log event: %s", message[:120], exc_info=True)


def prune(db: Session, now: datetime | None = None, retention_days: int | None = None) -> dict[str, int]:
    """Delete log lines older than SYSTEM_LOG_RETENTION_DAYS. Commits."""
    from sqlalchemy import delete

    from app.models import SystemLogEntry
    from app.settings import get_settings

    days = retention_days if retention_days is not None else get_settings().system_log_retention_days
    if days <= 0:
        return {"deleted": 0}
    cutoff = (now or utcnow()) - timedelta(days=days)
    deleted = db.execute(delete(SystemLogEntry).where(SystemLogEntry.created_at < cutoff)
                         .execution_options(synchronize_session=False)).rowcount or 0
    db.commit()
    return {"deleted": deleted}
