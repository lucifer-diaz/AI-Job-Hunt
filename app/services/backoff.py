"""What to do after a provider says 429: wait, and wait longer each time it says it again.

    call → 429 → cooldown for this account and provider
    every later call → held off until the cooldown passes, without touching the network
    first success → the cooldown is cleared and the ladder starts again

Without this, one rate limit turns into a burst of them: a hundred queued rankings all call the same model in
the same minute and all get rejected, which spends quota, fills the logs and delays the work further. The
cooldown is per account *and* per provider, so Apify being throttled never holds up an LLM call.

The provider's own `Retry-After` wins when it is longer than the computed delay (up to a sane cap): it knows
when it will serve again, and the ladder is only a guess.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.backoff")

MIN_BASE_SECONDS, MAX_BASE_SECONDS = 1, 600
MIN_MULTIPLIER, MAX_MULTIPLIER = 1.0, 10.0
MIN_MAX_SECONDS, MAX_MAX_SECONDS = 5, 86_400
MAX_EXPONENT = 12  # beyond this the cap has been reached anyway; keeps the arithmetic bounded
# A cooldown is read on the way into every external call, so the answer is cached for a moment. Short enough
# that a cooldown another worker set is picked up almost at once.
CACHE_SECONDS = 2.0


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class RateLimited(RuntimeError):
    """This account must leave the provider alone for a while longer."""

    def __init__(self, provider: str, retry_after_seconds: int):
        self.provider, self.retry_after_seconds = provider, retry_after_seconds
        super().__init__(f"{provider} rate limited this account; waiting {retry_after_seconds}s before calling again")


@dataclass(frozen=True)
class RateLimitPolicy:
    """How long to wait after the first, second, third… rate limit in a row."""

    enabled: bool = True
    base_seconds: int = 30
    multiplier: float = 2.0
    max_seconds: int = 900

    def delay(self, consecutive: int) -> timedelta:
        """The wait after `consecutive` rate limits in a row (1 = the first one)."""
        steps = min(MAX_EXPONENT, max(0, consecutive - 1))
        seconds = self.base_seconds * (self.multiplier ** steps)
        return timedelta(seconds=min(float(self.max_seconds), max(0.0, seconds)))

    def ladder(self, steps: int = 5) -> list[int]:
        """The first few waits, for the settings screen to show what the numbers mean."""
        return [round(self.delay(n).total_seconds()) for n in range(1, steps + 1)]

    def public(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "base_seconds": self.base_seconds, "multiplier": self.multiplier,
                "max_seconds": self.max_seconds, "ladder": self.ladder()}


def _bounded(value: Any, low: float, high: float, fallback: float, whole: bool = True) -> Any:
    try:
        number = min(high, max(low, float(value)))
    except (TypeError, ValueError):
        number = fallback
    return int(round(number)) if whole else round(number, 2)


def policy_from(saved: dict[str, Any] | None) -> RateLimitPolicy:
    """A policy from what an account saved, falling back to the server defaults for anything missing."""
    settings, saved = get_settings(), saved or {}
    return RateLimitPolicy(
        enabled=bool(saved.get("enabled", settings.rate_limit_backoff_enabled)),
        base_seconds=_bounded(saved.get("base_seconds", settings.rate_limit_base_seconds),
                              MIN_BASE_SECONDS, MAX_BASE_SECONDS, settings.rate_limit_base_seconds),
        multiplier=_bounded(saved.get("multiplier", settings.rate_limit_multiplier),
                            MIN_MULTIPLIER, MAX_MULTIPLIER, settings.rate_limit_multiplier, whole=False),
        max_seconds=_bounded(saved.get("max_seconds", settings.rate_limit_max_seconds),
                             MIN_MAX_SECONDS, MAX_MAX_SECONDS, settings.rate_limit_max_seconds))


def policy_for(db: Session, user_id: int | None = None) -> RateLimitPolicy:
    from app.services.user_settings import settings_for

    if user_id is None:
        return policy_from(None)
    return policy_from((settings_for(db, user_id).preferences or {}).get("rate_limits"))


# ───────────── the cooldowns themselves ─────────────

_sessions: Callable[[], Session] | None = None
_cache: dict[tuple[int, str], tuple[float, datetime | None]] = {}
_lock = threading.Lock()


def use_sessions(factory: Callable[[], Session] | None) -> None:
    global _sessions
    _sessions = factory


def _session_factory() -> Callable[[], Session]:
    if _sessions is not None:
        return _sessions
    from app.db import SessionLocal

    return SessionLocal


def reset_cache() -> None:
    """Forget what was cached. Used by tests and after a cooldown is cleared."""
    with _lock:
        _cache.clear()


def _cached(key: tuple[int, str], now: float) -> tuple[bool, datetime | None]:
    with _lock:
        entry = _cache.get(key)
    if entry is None or entry[0] <= now:
        return False, None
    return True, entry[1]


def _remember(key: tuple[int, str], until: datetime | None, now: float) -> None:
    with _lock:
        _cache[key] = (now + CACHE_SECONDS, until)


def cooling_until(user_id: int | None, provider: str, *, now: datetime | None = None) -> datetime | None:
    """When this account may call the provider again, or None when it may call it now. Never raises."""
    if user_id is None or not provider:
        return None
    key = (user_id, provider.upper())
    moment = now or utcnow()
    import time

    hit, cached = _cached(key, time.monotonic())
    if hit:
        return cached if cached and cached > moment else None
    try:
        from sqlalchemy import select

        from app.models import ProviderCooldown

        with _session_factory()() as db:
            until = db.scalar(select(ProviderCooldown.until).where(ProviderCooldown.user_id == key[0],
                                                                   ProviderCooldown.provider == key[1]))
    except Exception:  # noqa: BLE001  a backoff that cannot be read must not stop the call
        log.warning("could not read the %s cooldown", provider, exc_info=True)
        return None
    _remember(key, until, time.monotonic())
    return until if until and until > moment else None


def check(user_id: int | None, provider: str, *, now: datetime | None = None) -> None:
    """Raise RateLimited when this account is still cooling down for the provider."""
    until = cooling_until(user_id, provider, now=now)
    if until is None:
        return
    raise RateLimited(provider.upper(), max(1, round((until - (now or utcnow())).total_seconds())))


def record_limit(user_id: int | None, provider: str, *, retry_after: datetime | None = None,
                 policy: RateLimitPolicy | None = None, now: datetime | None = None) -> datetime | None:
    """Start or extend the cooldown after a 429. Returns when the provider may be called again."""
    if user_id is None or not provider:
        return None
    moment = now or utcnow()
    name = provider.upper()
    try:
        from sqlalchemy import select

        from app.models import ProviderCooldown

        with _session_factory()() as db:
            rule = policy if policy is not None else policy_for(db, user_id)
            if not rule.enabled:
                return None
            row = db.scalar(select(ProviderCooldown).where(ProviderCooldown.user_id == user_id,
                                                           ProviderCooldown.provider == name))
            if row is None:
                row = ProviderCooldown(user_id=user_id, provider=name, consecutive=0, calls_skipped=0)
                db.add(row)
            # Still inside the previous cooldown means a call slipped through; that is not a new rung.
            row.consecutive = row.consecutive + 1 if not row.until or row.until <= moment else max(1, row.consecutive)
            until = moment + rule.delay(row.consecutive)
            limit = get_settings().rate_limit_retry_after_max_seconds
            if retry_after is not None and moment < retry_after <= moment + timedelta(seconds=limit):
                until = max(until, retry_after)  # the provider knows better than the ladder does
            row.until, row.last_limited_at, row.updated_at = until, moment, moment
            db.commit()
        reset_cache()
        return until
    except Exception:  # noqa: BLE001  backoff bookkeeping must not break the call that triggered it
        log.warning("could not record the %s cooldown", provider, exc_info=True)
        return None


def note_success(user_id: int | None, provider: str) -> None:
    """Clear a cooldown once the provider answers normally again. Cheap when there is nothing to clear."""
    if user_id is None or not provider:
        return
    name = provider.upper()
    if cooling_until(user_id, name) is None and not _has_row(user_id, name):
        return
    try:
        from sqlalchemy import delete

        from app.models import ProviderCooldown

        with _session_factory()() as db:
            db.execute(delete(ProviderCooldown).where(ProviderCooldown.user_id == user_id,
                                                      ProviderCooldown.provider == name))
            db.commit()
        reset_cache()
    except Exception:  # noqa: BLE001
        log.warning("could not clear the %s cooldown", provider, exc_info=True)


def _has_row(user_id: int, provider: str) -> bool:
    try:
        from sqlalchemy import select

        from app.models import ProviderCooldown

        with _session_factory()() as db:
            return db.scalar(select(ProviderCooldown.id).where(ProviderCooldown.user_id == user_id,
                                                               ProviderCooldown.provider == provider)) is not None
    except Exception:  # noqa: BLE001
        return False


def note_skipped(user_id: int | None, provider: str) -> None:
    """Count a call the cooldown prevented, so Monitoring can show what the backoff saved."""
    if user_id is None or not provider:
        return
    try:
        from sqlalchemy import update

        from app.models import ProviderCooldown

        with _session_factory()() as db:
            db.execute(update(ProviderCooldown)
                       .where(ProviderCooldown.user_id == user_id, ProviderCooldown.provider == provider.upper())
                       .values(calls_skipped=ProviderCooldown.calls_skipped + 1)
                       .execution_options(synchronize_session=False))
            db.commit()
    except Exception:  # noqa: BLE001
        log.debug("could not count a skipped %s call", provider, exc_info=True)


def active(db: Session, user_id: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """The cooldowns running for this account, for the Monitoring screen."""
    from sqlalchemy import select

    from app.models import ProviderCooldown

    moment = now or utcnow()
    rows = db.scalars(select(ProviderCooldown).where(ProviderCooldown.user_id == user_id)
                      .order_by(ProviderCooldown.provider)).all()
    return [{"provider": row.provider, "until": row.until.isoformat() if row.until else None,
             "seconds_left": max(0, round((row.until - moment).total_seconds())) if row.until else 0,
             "consecutive": row.consecutive, "calls_skipped": row.calls_skipped,
             "last_limited_at": row.last_limited_at.isoformat() if row.last_limited_at else None}
            for row in rows if row.until and row.until > moment]


def reset(db: Session, user_id: int, provider: str | None = None) -> list[str]:
    """Clear this account's cooldowns (one provider, or all of them) because a person asked to, from Running workers.

    The ladder starts again from the bottom: the next 429 starts a fresh, short cooldown. Commits."""
    from sqlalchemy import delete, select

    from app.models import ProviderCooldown

    condition = [ProviderCooldown.user_id == user_id]
    if provider:
        condition.append(ProviderCooldown.provider == provider.upper())
    names = list(db.scalars(select(ProviderCooldown.provider).where(*condition)).all())
    if names:
        db.execute(delete(ProviderCooldown).where(*condition).execution_options(synchronize_session=False))
        db.commit()
    reset_cache()
    return sorted(names)


# A task a person asked to run now makes its calls even while its provider is cooling down (app.services.workers).
# Ambient, like the trace: the worker sets it around that one task and every external call underneath inherits it.
_bypass: ContextVar[bool] = ContextVar("jobhunt_backoff_bypass", default=False)


@contextmanager
def bypass(enabled: bool = True) -> Iterator[None]:
    """Skip cooldown checks inside the block. A 429 is still recorded, so the cooldown starts again if it recurs."""
    token = _bypass.set(bool(enabled) or _bypass.get())
    try:
        yield
    finally:
        _bypass.reset(token)


def bypassing() -> bool:
    return _bypass.get()
