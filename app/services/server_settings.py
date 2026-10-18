"""Server-wide worker settings an admin can change from Running workers → Settings, without a restart.

The scheduler jobs serve every account at once, so how often they run, how many tasks one run claims and how many run
side by side are server facts, not an account's. The environment (app.settings) sets them; an admin may override one
here, stored in `server_settings`. Deleting the override (saving the environment's value again) hands it back.

Reads are cached for a few seconds and never raise: if the table cannot be read, the environment value applies.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import SchedulerJob, ServerSetting
from app.settings import get_settings

log = logging.getLogger(__name__)
CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class Editable:
    name: str  # the app.settings attribute it overrides
    worker: str  # the Running workers tab it belongs to
    label: str
    help: str
    low: int
    high: int
    scheduler_job: str | None = None  # an interval: the scheduler job it paces


EDITABLE: dict[str, Editable] = {e.name: e for e in (
    Editable("jd_parse_interval_minutes", "parsing", "Run every (minutes)",
             "How often the JD parsing job looks for work. 0 turns it off for every account.", 0, 1440, "jd_parsing"),
    Editable("jd_parse_batch_size", "parsing", "Tasks per run", "How many parse tasks one run claims.", 1, 200),
    Editable("jd_parse_concurrency", "parsing", "Side by side", "How many postings are parsed at once.", 1, 32),
    Editable("jd_recovery_interval_minutes", "recovery", "Run every (minutes)",
             "How often JD recovery looks for work. 0 turns it off for every account.", 0, 1440, "jd_recovery"),
    Editable("jd_fetch_batch_size", "recovery", "Tasks per run", "How many recovery tasks one run claims.", 1, 200),
    Editable("jd_fetch_max_concurrency", "recovery", "Side by side", "How many postings are fetched at once.", 1, 16),
    Editable("job_insights_interval_minutes", "insights", "Run every (minutes)",
             "How often Job insights looks for work. 0 turns it off for every account.", 0, 1440, "job_insights"),
    Editable("job_insights_batch_size", "insights", "Tasks per run", "How many jobs one run researches.", 1, 100),
    Editable("job_insights_max_concurrency", "insights", "Side by side", "How many jobs are researched at once.", 1, 8),
    Editable("ranking_interval_minutes", "ranking", "Run every (minutes)",
             "How often the ranking job looks for work. 0 turns it off for every account.", 0, 1440, "ranking"),
    Editable("ranking_batch_size", "ranking", "Tasks per run", "How many ranking tasks one run claims.", 1, 500),
    Editable("ranking_concurrency", "ranking", "Most per account",
             "The most rankings one account may run at once (each account picks up to this).", 1, 64),
    Editable("tailor_concurrency", "tailoring", "Side by side",
             "How many packets are tailored at once across every account, and the most one account may ask for "
             "(each account picks up to this).", 1, 16),
    Editable("rag_index_interval_minutes", "indexing", "Run every (minutes)",
             "How often the retrieval indexer looks for work. 0 turns it off for every account; it never runs while "
             "RAG_VECTOR_ENABLED is off.", 0, 1440, "rag_index"),
    Editable("rag_embed_batch_size", "indexing", "Entries per batch",
             "How many facts or answers go to the embedding model in one request.", 1, 256),
    Editable("rag_index_max_entries_per_run", "indexing", "Entries per account per run",
             "The most entries one run embeds for one account.", 1, 5000),
    Editable("rag_index_concurrency", "indexing", "Side by side",
             "How many embedding batches are in flight at once, across accounts.", 1, 16),
)}

_lock = threading.Lock()
_cache: dict[str, int] = {}
_cached_at = 0.0


def _session() -> Session:
    from app.db import SessionLocal

    return SessionLocal()


def overrides() -> dict[str, int]:
    """Every saved override, cached briefly. Never raises."""
    global _cache, _cached_at
    with _lock:
        if time.monotonic() - _cached_at < CACHE_SECONDS:
            return dict(_cache)
    try:
        with _session() as db:
            rows = dict(db.execute(select(ServerSetting.name, ServerSetting.value)).all())
    except Exception:  # noqa: BLE001  a missing table or a dropped connection: the environment decides
        log.debug("server settings unavailable", exc_info=True)
        rows = {}
    with _lock:
        _cache, _cached_at = {k: int(v) for k, v in rows.items() if k in EDITABLE}, time.monotonic()
        return dict(_cache)


def reset_cache() -> None:
    global _cached_at
    with _lock:
        _cached_at = 0.0


def value(name: str) -> int:
    """The setting in force: the admin's override, else the environment's."""
    saved = overrides().get(name)
    return saved if saved is not None else int(getattr(get_settings(), name))


def interval_override(setting_name: str) -> int | None:
    """A scheduler interval an admin set, or None (the scheduler keeps its own)."""
    return overrides().get(setting_name) if setting_name in EDITABLE else None


def describe() -> dict[str, dict[str, Any]]:
    saved = overrides()
    settings = get_settings()
    return {name: {"worker": e.worker, "label": e.label, "help": e.help, "min": e.low, "max": e.high,
                   "value": saved.get(name, int(getattr(settings, name))), "default": int(getattr(settings, name)),
                   "overridden": name in saved}
            for name, e in EDITABLE.items()}


class ServerSettingError(ValueError):
    pass


def update_values(db: Session, changes: dict[str, Any], actor: str) -> dict[str, dict[str, Any]]:
    """Save an admin's changes. A value equal to the environment's removes the override. Commits."""
    settings = get_settings()
    clean: dict[str, int] = {}
    for name, raw in changes.items():
        spec = EDITABLE.get(name)
        if spec is None:
            raise ServerSettingError(f"'{name}' cannot be changed here")
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise ServerSettingError(f"{spec.label}: enter a whole number") from None
        if not spec.low <= number <= spec.high:
            raise ServerSettingError(f"{spec.label}: between {spec.low} and {spec.high}")
        clean[name] = number
    now = datetime.now(UTC).replace(tzinfo=None)
    for name, number in clean.items():
        db.execute(delete(ServerSetting).where(ServerSetting.name == name))
        if number != int(getattr(settings, name)):
            db.add(ServerSetting(name=name, value=number, updated_by=actor[:200], updated_at=now))
        job = EDITABLE[name].scheduler_job
        if job:  # a new interval applies from now, not after the old one has run out
            db.execute(update(SchedulerJob).where(SchedulerJob.name == job)
                       .values(next_run_at=None if number <= 0 else now))
    db.commit()
    reset_cache()
    return describe()
