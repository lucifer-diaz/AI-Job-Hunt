"""In-process scheduler for Job Hunt OS, started and stopped with FastAPI.

Every app instance runs the same loop. Before a job runs, the instance takes that job's row in
`scheduler_jobs` with one conditional UPDATE, which row-locks it: the update only succeeds when the
job is due and no other instance holds a live lease. Two instances therefore never run the same job
at the same time, a job runs at most once per interval, and a lease left by a crashed instance
expires so another instance takes over.

Scheduled runs, manual admin triggers and `python -m app.cron` all go through `Scheduler.run_job`,
which calls the same job function from app.services.scheduled_jobs. A job that raises is recorded
as failed and never affects another job or the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import SchedulerJob
from app.services import system_log, tracing
from app.services.scheduled_jobs import JOBS, JobContext, JobSpec
from app.settings import get_settings

log = logging.getLogger("jobhunt.scheduler")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class RunOutcome:
    job: str
    trigger: str
    status: str  # succeeded | failed | skipped | busy | not_due
    started_at: datetime | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {"job": self.job, "trigger": self.trigger, "status": self.status,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "duration_ms": self.duration_ms, "result": self.result, "error": self.error}


def _json_safe(value: Any) -> dict[str, Any]:
    return json.loads(json.dumps(value if isinstance(value, dict) else {"value": value}, default=str))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Scheduler:
    def __init__(self, specs: list[JobSpec], sessions: Callable[[], Session], *, intervals: dict[str, int],
                 enabled: bool = True, tick_seconds: float = 30, lease_seconds: int = 1800,
                 instance_id: str | None = None, clock: Callable[[], datetime] = utcnow):
        self.specs = {spec.name: spec for spec in specs}
        self.sessions = sessions
        self.intervals = dict(intervals)
        self.enabled = enabled
        self.tick_seconds = tick_seconds
        self.lease_seconds = lease_seconds
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.clock = clock
        self.started_at: datetime | None = None
        self._loop_task: asyncio.Task | None = None
        self._inflight: dict[str, asyncio.Task] = {}

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    def interval(self, name: str) -> int:
        return max(0, int(self.intervals.get(name) or 0))

    # ───────────── lifecycle ─────────────

    async def start(self) -> None:
        if self.running:
            return
        await asyncio.to_thread(self.ensure_rows)
        self.started_at = self.clock()
        self._loop_task = asyncio.create_task(self._loop(), name="jobhunt-scheduler")
        log.info("scheduler started on %s", self.instance_id)

    async def stop(self, timeout: float = 10.0) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        pending = [task for task in self._inflight.values() if not task.done()]
        if pending:
            # Jobs run in threads and cannot be interrupted. Anything still running keeps its database
            # lease until it finishes or the lease expires, so no work is lost or run twice.
            await asyncio.wait(pending, timeout=timeout)
        self._inflight = {name: task for name, task in self._inflight.items() if not task.done()}
        self.started_at = None
        log.info("scheduler stopped on %s", self.instance_id)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # never let one bad tick end the loop
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self) -> list[str]:
        """Start every enabled job that is not already running in this instance; the lease decides if it runs."""
        started = []
        for name in self.specs:
            if self.interval(name) <= 0:
                continue
            task = self._inflight.get(name)
            if task is not None and not task.done():
                continue
            self._inflight[name] = asyncio.create_task(self.run_job(name, "schedule"), name=f"job-{name}")
            started.append(name)
        return started

    # ───────────── running a job ─────────────

    async def run_job(self, name: str, trigger: str = "manual") -> RunOutcome:
        spec = self.specs.get(name)
        if spec is None:
            raise KeyError(name)
        owner = f"{self.instance_id}:{uuid.uuid4().hex[:8]}"
        now = self.clock()
        claimed, code, reason = await asyncio.to_thread(self._claim, name, owner, now, trigger)
        if not claimed:
            return RunOutcome(name, trigger, code, error=reason)

        ctx = JobContext(sessions=self.sessions, trigger=trigger, now=now, worker_id=owner,
                         interval_minutes=self.interval(name))
        started = time.monotonic()
        result, error = None, None
        # One correlation ID for the whole job: every external call it makes, in any account, is findable
        # together afterwards (app.services.tracing). asyncio.to_thread copies the context, so worker threads
        # inherit it.
        with tracing.trace(name, workflow_type="system", inherit=False) as trace_ctx:
            try:
                if asyncio.iscoroutinefunction(spec.run):
                    value = await spec.run(ctx)
                else:
                    value = await asyncio.to_thread(spec.run, ctx)
                result = _json_safe(value)
                status = "skipped" if isinstance(result.get("skipped"), str) else "succeeded"
            except Exception as exc:  # recorded on the job; other jobs and the loop carry on
                log.exception("scheduled job %s failed", name)
                status, error = "failed", f"{type(exc).__name__}: {exc}"[:4000]
            duration_ms = int((time.monotonic() - started) * 1000)
            await asyncio.to_thread(self._log_run, name, trigger, status, error, result, duration_ms,
                                    trace_ctx.trace_id)
        try:
            await asyncio.to_thread(self._release, name, owner, now, status, error, result, duration_ms)
        except Exception:  # the lease expires on its own if the database is unreachable
            log.exception("could not record the result of scheduled job %s", name)
        return RunOutcome(name, trigger, status, now, duration_ms, result, error)

    @staticmethod
    def _log_run(name: str, trigger: str, status: str, error: str | None, result: dict[str, Any] | None,
                 duration_ms: int, trace_id: str) -> None:
        """One Monitoring → Logs line per scheduled run, so a job's history reads next to the calls it made."""
        system_log.log_event(
            category="SCHEDULER", level="ERROR" if status == "failed" else "INFO",
            message=f"{name} {status}" + (f": {error}" if error else ""),
            operation=name, workflow=name, trace_id=trace_id, status=status, duration_ms=duration_ms,
            details={"trigger": trigger, "result": result})

    def ensure_rows(self) -> None:
        with self.sessions() as db:
            for name in self.specs:
                self._ensure_row(db, name)

    @staticmethod
    def _ensure_row(db: Session, name: str) -> None:
        if db.get(SchedulerJob, name) is not None:
            return
        try:
            db.add(SchedulerJob(name=name))
            db.commit()
        except IntegrityError:
            db.rollback()  # another instance created it first

    def _claim(self, name: str, owner: str, now: datetime, trigger: str) -> tuple[bool, str, str]:
        with self.sessions() as db:
            self._ensure_row(db, name)
            conditions = [SchedulerJob.name == name,
                          or_(SchedulerJob.lock_expires_at.is_(None), SchedulerJob.lock_expires_at <= now)]
            if trigger == "schedule":  # manual and cron runs do not wait for the interval
                conditions.append(or_(SchedulerJob.next_run_at.is_(None), SchedulerJob.next_run_at <= now))
            result = db.execute(update(SchedulerJob).where(*conditions).values(
                locked_by=owner[:200], locked_at=now, lock_expires_at=now + timedelta(seconds=self.lease_seconds),
                last_started_at=now, last_status="running", last_trigger=trigger, updated_at=now,
            ).execution_options(synchronize_session=False))
            db.commit()
            if result.rowcount == 1:
                return True, "", ""
            row = db.get(SchedulerJob, name)
            if row.lock_expires_at and row.lock_expires_at > now:
                holder = (row.locked_by or "").rsplit(":", 1)[0]
                return False, "busy", f"{name} is already running on {holder} until {row.lock_expires_at.isoformat()}"
            return False, "not_due", f"{name} is not due until {_iso(row.next_run_at)}"

    def _release(self, name: str, owner: str, started: datetime, status: str, error: str | None,
                 result: dict[str, Any] | None, duration_ms: int) -> None:
        interval = self.interval(name)
        finished = self.clock()
        with self.sessions() as db:
            db.execute(update(SchedulerJob).where(SchedulerJob.name == name, SchedulerJob.locked_by == owner[:200]).values(
                locked_by=None, locked_at=None, lock_expires_at=None, last_finished_at=finished,
                last_status=status, last_error=error, last_result=result, last_duration_ms=duration_ms,
                run_count=SchedulerJob.run_count + 1,
                failure_count=SchedulerJob.failure_count + (1 if status == "failed" else 0),
                next_run_at=started + timedelta(minutes=interval) if interval > 0 else None, updated_at=finished,
            ).execution_options(synchronize_session=False))
            db.commit()

    # ───────────── status ─────────────

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self.sessions() as db:
            rows = {row.name: row for row in db.scalars(select(SchedulerJob)).all()}
        jobs = []
        for name, spec in self.specs.items():
            row, interval, task = rows.get(name), self.interval(name), self._inflight.get(name)
            live = bool(row and row.lock_expires_at and row.lock_expires_at > now)
            last_status = row.last_status if row else None
            if last_status == "running" and not live:
                last_status = "interrupted"  # the instance running it stopped before recording a result
            jobs.append({
                "name": name, "title": spec.title, "description": spec.description, "interval_minutes": interval,
                "enabled": self.enabled and interval > 0, "running": live,
                "running_here": bool(task is not None and not task.done()),
                "locked_by": row.locked_by if live else None, "lock_expires_at": _iso(row.lock_expires_at) if live else None,
                "next_run_at": _iso(row.next_run_at) if row else None,
                "last_started_at": _iso(row.last_started_at) if row else None,
                "last_finished_at": _iso(row.last_finished_at) if row else None,
                "last_status": last_status, "last_trigger": row.last_trigger if row else None,
                "last_error": row.last_error if row else None,
                "last_duration_ms": row.last_duration_ms if row else None,
                "last_result": row.last_result if row else None,
                "run_count": row.run_count if row else 0, "failure_count": row.failure_count if row else 0,
            })
        return {"enabled": self.enabled, "running": self.running, "instance_id": self.instance_id,
                "started_at": _iso(self.started_at), "tick_seconds": self.tick_seconds,
                "lease_seconds": self.lease_seconds, "jobs": jobs}


def build_scheduler(sessions: Callable[[], Session] | None = None, **overrides: Any) -> Scheduler:
    """The app's scheduler, configured from settings."""
    from app.db import SessionLocal

    settings = get_settings()
    options: dict[str, Any] = {
        "intervals": {spec.name: getattr(settings, spec.interval_setting) for spec in JOBS},
        "enabled": settings.scheduler_enabled,
        "tick_seconds": settings.scheduler_tick_seconds,
        "lease_seconds": settings.scheduler_lease_minutes * 60,
    }
    options.update(overrides)
    return Scheduler(JOBS, sessions or SessionLocal, **options)
