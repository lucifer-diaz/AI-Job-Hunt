"""An account's own failover thresholds (AI Orchestration → Tiers & routing → Failover).

Stored in AppSetting.preferences["failover"]. A value the account never set follows the default below, so nothing
changes for an account that never opens the screen. Failover is on by default but only acts on a worker that has a
secondary model (a fallback or `secondary_models`); a worker with none behaves exactly as before.

Every reader here is cheap and never raises: a broken or missing value falls back to the default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting

ALGORITHMS = {
    "PRIORITY": "Configured order: the first healthy model in the chain",
    "LATENCY": "Fastest: the healthy model with the lowest recent p95 latency",
    "LEAST_ERRORS": "Most reliable: the healthy model with the lowest recent failure rate",
    "WEIGHTED": "Balanced: the best blend of reliability, speed and schema quality",
}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    help: str
    kind: str  # "bool", "int" or "choice"
    default: Any
    low: int = 0
    high: int = 0


FIELDS: tuple[Field, ...] = (
    Field("enabled", "Automatic failover", "Off: models are never switched automatically.", "bool", True),
    Field("algorithm", "Routing algorithm", "How the next model is chosen when the primary is unhealthy.", "choice",
          "PRIORITY"),
    Field("window_minutes", "Look-back window (minutes)", "Recent requests older than this are ignored.", "int", 60, 5,
          1440),
    Field("min_samples", "Minimum requests", "Below this many requests a route is never judged: unknown stays healthy.",
          "int", 5, 1, 1000),
    Field("max_failure_pct", "Failure rate limit (%)", "Switch away when more than this share of requests fail.", "int",
          50, 1, 100),
    Field("max_rate_limits", "Rate limits in the window", "Switch away after this many 429s in the window.", "int", 3,
          1, 100),
    Field("consecutive_failures", "Failures in a row", "Switch away immediately after this many failures in a row.",
          "int", 4, 1, 50),
    Field("max_schema_failure_pct", "Invalid output limit (%)",
          "Switch away when more than this share of answers fail their output contract.", "int", 60, 1, 100),
    Field("max_p95_ms", "Slowest acceptable p95 (ms, 0 = off)", "Switch away when the 95th percentile latency is higher.",
          "int", 0, 0, 3_600_000),
    Field("degradation_pct", "Slower than its own history (%, 0 = off)",
          "Switch away when p95 latency exceeds the route's own 7-day median by this much.", "int", 0, 0, 10_000),
    Field("cooldown_seconds", "First cooldown (seconds)", "How long a failed model rests; doubles each time.", "int",
          60, 5, 3600),
    Field("max_cooldown_seconds", "Longest cooldown (seconds)", "The doubling stops here.", "int", 900, 5, 86_400),
    Field("recovery_successes", "Successes to recover", "Probe requests that must succeed before the primary returns.",
          "int", 2, 1, 20),
    Field("notify", "Notify me when a model switches", "Browser notification (and in-app message) on every switch.",
          "bool", True),
)
BY_NAME = {f.name: f for f in FIELDS}
DEFAULTS: dict[str, Any] = {f.name: f.default for f in FIELDS}


class FailoverSettingError(ValueError):
    pass


def _saved(preferences: dict[str, Any] | None) -> dict[str, Any]:
    stored = (preferences or {}).get("failover")
    return stored if isinstance(stored, dict) else {}


def resolve(stored: dict[str, Any] | None) -> dict[str, Any]:
    stored = stored or {}
    out: dict[str, Any] = {}
    for f in FIELDS:
        raw = stored.get(f.name)
        if f.kind == "bool":
            out[f.name] = raw if isinstance(raw, bool) else f.default
        elif f.kind == "choice":
            out[f.name] = raw if raw in ALGORITHMS else f.default
        else:
            try:
                out[f.name] = max(f.low, min(f.high, int(raw))) if raw is not None else f.default
            except (TypeError, ValueError):
                out[f.name] = f.default
    out["max_cooldown_seconds"] = max(out["max_cooldown_seconds"], out["cooldown_seconds"])
    return out


def for_user(db: Session, user_id: int) -> dict[str, Any]:
    """The account's failover settings, defaults filled in. Never raises."""
    try:
        prefs = db.scalar(select(AppSetting.preferences).where(AppSetting.user_id == user_id)) or {}
        return resolve(_saved(prefs))
    except Exception:  # noqa: BLE001  a settings read never stops a request
        return resolve({})


def describe(db: Session, user_id: int) -> dict[str, Any]:
    return {"values": for_user(db, user_id),
            "algorithms": [{"name": k, "label": v} for k, v in ALGORITHMS.items()],
            "fields": [{"name": f.name, "label": f.label, "help": f.help, "type": f.kind, "min": f.low, "max": f.high,
                        "default": f.default} for f in FIELDS]}


def update(db: Session, user_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """Save an account's changes. A value equal to the default is stored as "follow the default". Commits."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.user_settings import lock_settings_row, settings_for

    clean: dict[str, Any] = {}
    for name, raw in (changes or {}).items():
        f = BY_NAME.get(name)
        if f is None:
            raise FailoverSettingError(f"'{name}' is not a failover setting")
        if f.kind == "bool":
            if not isinstance(raw, bool):
                raise FailoverSettingError(f"{f.label}: on or off")
            value: Any = raw
        elif f.kind == "choice":
            if raw not in ALGORITHMS:
                raise FailoverSettingError(f"{f.label}: one of {', '.join(ALGORITHMS)}")
            value = raw
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise FailoverSettingError(f"{f.label}: enter a whole number") from None
            if not f.low <= value <= f.high:
                raise FailoverSettingError(f"{f.label}: between {f.low} and {f.high}")
        clean[name] = None if value == f.default else value
    row = lock_settings_row(db, settings_for(db, user_id))
    prefs = dict(row.preferences or {})
    merged = {**_saved(prefs), **clean}
    first = merged.get("cooldown_seconds") or DEFAULTS["cooldown_seconds"]
    longest = merged.get("max_cooldown_seconds") or DEFAULTS["max_cooldown_seconds"]
    if first > longest:
        raise FailoverSettingError("First cooldown: it cannot be longer than the longest cooldown")
    prefs["failover"] = {k: v for k, v in merged.items() if v is not None}
    row.preferences = prefs
    flag_modified(row, "preferences")
    db.commit()
    return describe(db, user_id)
