"""The thread pools background work runs in, so slow model calls can never starve the scheduler.

`asyncio.to_thread` shares one small default pool (min(32, CPUs + 4) threads) with everything else in the process.
Model calls can take minutes (LLM_REQUEST_TIMEOUT_SECONDS), and a call whose task was stopped from Running workers
keeps its thread until the provider answers, because threads cannot be interrupted. With every thread of that one pool
busy, the scheduler could not even claim or release a job's lease, and every queue stalled behind the slowest call.

So the work is split:

* `run_work`: the long, blocking work of the queued workers (a ranking, a parse, a tailoring run, a scheduled job's
  body). A large pool; threads are created only when needed.
* `run_io`: the scheduler's own short bookkeeping (claiming and releasing job leases, claiming tasks, renewing their
  leases, logging a run). A separate pool that long work can never fill.

Both copy the caller's context, as `asyncio.to_thread` does, so traces (app.services.tracing) and a Run now's cooldown
bypass (app.services.backoff) reach the thread.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

WORK_THREADS = 64
IO_THREADS = 8

_work = ThreadPoolExecutor(max_workers=WORK_THREADS, thread_name_prefix="jobhunt-work")
_io = ThreadPoolExecutor(max_workers=IO_THREADS, thread_name_prefix="jobhunt-io")


async def _run(pool: ThreadPoolExecutor, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(pool, functools.partial(context.run, fn, *args, **kwargs))


async def run_work(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Long, blocking worker work: model calls, a scheduled job's body."""
    return await _run(_work, fn, *args, **kwargs)


async def run_io(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Short scheduler bookkeeping that must never wait behind a model call."""
    return await _run(_io, fn, *args, **kwargs)
