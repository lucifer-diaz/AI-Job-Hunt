"""Request-side hooks into the in-app scheduler (app.scheduler), kept free of imports so any router can use them."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# Wake-up hints. A worker (the scheduler's jobs, the tailor worker) registers how to wake it; any code, on any thread,
# calls `nudge(name)` after it queued work for that worker. The task tables stay the only queue: a nudge only makes
# the next pass start now instead of at the next tick, so a lost or repeated one changes nothing.
_wakers: dict[str, Callable[[], None]] = {}


def register(name: str, wake: Callable[[], None]) -> None:
    _wakers[name] = wake


def unregister(name: str, wake: Callable[[], None] | None = None) -> None:
    if wake is None or _wakers.get(name) is wake:
        _wakers.pop(name, None)


def nudge(name: str) -> None:
    """Ask the worker `name` to look for work now. Never raises and never blocks."""
    wake = _wakers.get(name)
    if wake is not None:
        try:
            wake()
        except Exception:  # a hint must never change the outcome of the work that sent it
            log.exception("could not nudge %s", name)


def run_soon(background: Any, app: Any, name: str) -> None:
    """After a request queues work, run that job once the response is sent instead of waiting for its interval.

    `background` is the request's FastAPI BackgroundTasks. The run takes the same database lease as the scheduler,
    so it never overlaps a scheduled run; it does nothing when the scheduler is disabled.
    """
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None and scheduler.enabled and scheduler.interval(name) > 0:
        background.add_task(scheduler.run_job, name, "manual")
