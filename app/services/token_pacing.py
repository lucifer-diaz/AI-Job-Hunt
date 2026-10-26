"""Hold a model request that the provider's per-minute token window certainly cannot take, until the window resets.

    response → x-ratelimit-{limit,remaining,reset}-tokens → observe(): one provider_rate_windows row per account and
               LITELLM:<model>, overwritten by every answer
    request  → reserve(need): enough left → the need is reserved on the row and the request goes
                              not enough left before the reset → wait (external_api.hold_off raises RateLimitedCooldown)
    answer   → observe(released=need) under the model that answered; a failure → release(need)

A provider such as Groq's Free plan allows ~8K tokens a minute; with ~6K-token prompts, every second request would
otherwise be a 429 that starts a cooldown and rests the model. The window is what the provider said, never a guess:
no row, a row whose reset has passed, or a request larger than the whole limit (it can never fit: the provider's
refusal says so once, see external_api.too_large) all send the request.

PostgreSQL is the only shared state: a reservation is one guarded UPDATE, so workers in other processes see it at
once. Fails open: if the window cannot be read or written, the request is sent.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.token_pacing")

PREFIX = "LITELLM:"  # only model requests through the gateway are paced (external_api.cooldown_provider)
# What a read-only check (failover's ordering) may reuse; a reservation always reads the database.
CACHE_SECONDS = 2.0


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Window:
    """What one response said about the account's limits. None means the provider did not say."""

    tokens_limit: int | None = None
    tokens_remaining: int | None = None
    tokens_reset_seconds: float | None = None
    requests_limit: int | None = None
    requests_remaining: int | None = None
    requests_reset_seconds: float | None = None
    requests_window: str | None = None  # "day" (Groq), "minute", or None when unknown

    def paceable(self) -> bool:
        return self.tokens_remaining is not None and self.tokens_reset_seconds is not None


_sessions: Callable[[], Session] | None = None
_cache: dict[tuple[int, str], tuple[float, tuple[int | None, int | None, datetime | None] | None]] = {}
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
    with _lock:
        _cache.clear()


def paced(key: str | None) -> bool:
    return bool(key) and key.upper().startswith(PREFIX)


def observe(user_id: int | None, key: str, window: Window, *, released: int = 0,
            now: datetime | None = None) -> None:
    """Store what the provider just said about this model's window. Never raises.

    The provider's figure counts only requests it has already seen, so the reservations of requests still in flight
    are kept off it: `released` is what the request that brought this answer had reserved. A window whose reset has
    passed forgets its reservations (a request lost in flight cannot hold tokens for ever)."""
    if user_id is None or not paced(key) or not window.paceable():
        return
    moment = now or utcnow()
    name = key[:230]
    try:
        from sqlalchemy import select

        from app.models import ProviderRateWindow

        with _session_factory()() as db:
            row = db.scalar(select(ProviderRateWindow).where(ProviderRateWindow.user_id == user_id,
                                                             ProviderRateWindow.provider == name)
                            .with_for_update())  # a reservation made meanwhile is not lost
            if row is None:
                row = ProviderRateWindow(user_id=user_id, provider=name, tokens_reserved=0)
                db.add(row)
            current = row.tokens_reset_at is not None and row.tokens_reset_at > moment
            row.tokens_reserved = max(0, (row.tokens_reserved or 0) - max(0, released)) if current else 0
            row.tokens_limit = window.tokens_limit
            row.tokens_remaining = max(0, (window.tokens_remaining or 0) - row.tokens_reserved)
            row.tokens_reset_at = moment + timedelta(seconds=max(0.0, window.tokens_reset_seconds or 0.0))
            row.requests_limit, row.requests_remaining = window.requests_limit, window.requests_remaining
            row.requests_reset_at = (moment + timedelta(seconds=max(0.0, window.requests_reset_seconds))
                                     if window.requests_reset_seconds is not None else None)
            row.requests_window = window.requests_window
            row.observed_at = row.updated_at = moment
            db.commit()
        reset_cache()
    except Exception:  # noqa: BLE001  pacing is an optimisation; it must never fail the call that fed it
        log.warning("could not record the %s token window", key, exc_info=True)


def release(user_id: int | None, key: str, amount: int | None, *, spent: bool = False) -> None:
    """Settle a reservation whose request brought no window of this model: given back when it failed or another
    model answered, or — `spent`, answered without rate-limit headers — no longer in flight but still spent.
    Never raises."""
    if user_id is None or not paced(key) or not isinstance(amount, int) or amount <= 0:
        return
    try:
        from sqlalchemy import case, update

        from app.models import ProviderRateWindow

        row = ProviderRateWindow
        with _session_factory()() as db:
            db.execute(update(row).where(row.user_id == user_id, row.provider == key[:230])
                       .values(tokens_reserved=case((row.tokens_reserved > amount, row.tokens_reserved - amount),
                                                    else_=0),
                               tokens_remaining=row.tokens_remaining if spent else row.tokens_remaining + case(
                                   (row.tokens_reserved > amount, amount), else_=row.tokens_reserved))
                       .execution_options(synchronize_session=False))
            db.commit()
        reset_cache()
    except Exception:  # noqa: BLE001
        log.warning("could not release tokens on the %s window", key, exc_info=True)


def _wait(remaining: int | None, limit: int | None, reset_at: datetime | None, need: int,
          moment: datetime) -> int | None:
    """Seconds to wait, or None to send: only a known, unexpired window too small for a request that fits it waits."""
    if remaining is None or reset_at is None or reset_at <= moment or remaining >= need:
        return None
    if limit is not None and need > limit:
        return None  # it can never fit: waiting cannot help, and the provider's refusal says so once
    return max(1, math.ceil((reset_at - moment).total_seconds()))


def reserve(user_id: int | None, key: str, need: int | None, *,
            now: datetime | None = None) -> tuple[int | None, int]:
    """Take `need` tokens off the model's window: (None, tokens reserved) to send it, (seconds, 0) to wait first.
    The reservation stays on the window until the request's answer (`observe`) or failure (`release`).

    Fails open: any error is (None, 0), send."""
    if user_id is None or not paced(key) or not isinstance(need, int) or need <= 0:
        return None, 0
    moment = now or utcnow()
    name = key[:230]
    try:
        from sqlalchemy import select, update

        from app.models import ProviderRateWindow

        where = (ProviderRateWindow.user_id == user_id, ProviderRateWindow.provider == name)
        with _session_factory()() as db:
            taken = db.execute(update(ProviderRateWindow)
                               .where(*where, ProviderRateWindow.tokens_reset_at > moment,
                                      ProviderRateWindow.tokens_remaining >= need)
                               .values(tokens_remaining=ProviderRateWindow.tokens_remaining - need,
                                       tokens_reserved=ProviderRateWindow.tokens_reserved + need)
                               .execution_options(synchronize_session=False))
            db.commit()
            if taken.rowcount:
                reset_cache()
                return None, need
            row = db.execute(select(ProviderRateWindow.tokens_remaining, ProviderRateWindow.tokens_limit,
                                    ProviderRateWindow.tokens_reset_at).where(*where)).first()
        return (_wait(row[0], row[1], row[2], need, moment) if row else None), 0
    except Exception:  # noqa: BLE001  a window that cannot be read must not stop the call
        log.warning("could not reserve tokens on the %s window", key, exc_info=True)
        return None, 0


def waiting(user_id: int | None, key: str, need: int | None, *, now: datetime | None = None) -> int | None:
    """Read-only: seconds this request would wait on the model's window, or None. For failover's ordering; cached
    for a moment. Never raises."""
    if user_id is None or not paced(key) or not isinstance(need, int) or need <= 0:
        return None
    moment = now or utcnow()
    cache_key = (user_id, key[:230])
    clock = time.monotonic()
    with _lock:
        hit = _cache.get(cache_key)
    if hit is not None and hit[0] > clock:
        state = hit[1]
    else:
        try:
            from sqlalchemy import select

            from app.models import ProviderRateWindow

            with _session_factory()() as db:
                row = db.execute(select(ProviderRateWindow.tokens_remaining, ProviderRateWindow.tokens_limit,
                                        ProviderRateWindow.tokens_reset_at)
                                 .where(ProviderRateWindow.user_id == user_id,
                                        ProviderRateWindow.provider == cache_key[1])).first()
        except Exception:  # noqa: BLE001
            log.warning("could not read the %s token window", key, exc_info=True)
            return None
        state = (row[0], row[1], row[2]) if row else None
        with _lock:
            _cache[cache_key] = (clock + CACHE_SECONDS, state)
    return _wait(state[0], state[1], state[2], need, moment) if state else None

