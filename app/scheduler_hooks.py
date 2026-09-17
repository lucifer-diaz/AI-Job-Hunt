"""Request-side hooks into the in-app scheduler (app.scheduler), kept free of imports so any router can use them."""
from __future__ import annotations

from typing import Any


def run_soon(background: Any, app: Any, name: str) -> None:
    """After a request queues work, run that job once the response is sent instead of waiting for its interval.

    `background` is the request's FastAPI BackgroundTasks. The run takes the same database lease as the scheduler,
    so it never overlaps a scheduled run; it does nothing when the scheduler is disabled.
    """
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None and scheduler.enabled and scheduler.interval(name) > 0:
        background.add_task(scheduler.run_job, name, "manual")
