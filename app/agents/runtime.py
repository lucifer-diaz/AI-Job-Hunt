"""Running a graph thread: start, execute under a lease, interrupt, resume, stop, recover after a crash, prune.

    start(db, "pilot", input)             → an agent_threads row (QUEUED), owned by the session's account
    claim(...) → RunHandle                → the lease (guarded UPDATE on lock_token), RUNNING
    execute(sessions, run, command)       → LangGraph invoke on the PostgreSQL checkpointer; the row ends
                                            COMPLETED, INTERRUPTED (waiting for a person) or FAILED, under the lease
    resume(db, id, decision)              → a validated decision for an INTERRUPTED thread, then execute
    recover(sessions, ...)                → the scheduler pass: threads whose lease expired (a worker died) continue
                                            from their last checkpoint; repeated effects are prevented by effects.once

Ownership is checked before any checkpointer access, on start and on every resume (app.agents.threads). The lease is
renewed while a run is in progress; a stopped thread loses it and its run stops at the next guarded write. No
setting, key or credential is read to resume a thread: the checkpoint and the decision are all a run gets.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import checkpoint, pilot, safety, threads, tracing_boundary
from app.agents.effects import LeaseLost, RunHandle
from app.models import AgentThread
from app.services import tracing
from app.services.lifecycle import STOPPED_BY_USER
from app.services.redaction import redact_text
from app.settings import get_settings
from app.tenancy import require_user_id

log = logging.getLogger("jobhunt.agents")
ERROR_LIMIT = 300
EXPIRED_NOTE = "Expired while waiting for a person"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class GraphSpec:
    name: str
    version: str
    build: Callable[[Any], Any]
    input_model: type[BaseModel]
    decision_model: type[BaseModel]
    summary: Callable[[dict[str, Any]], dict[str, Any]]
    export_fields: tuple[str, ...]


GRAPHS: dict[str, GraphSpec] = {
    pilot.GRAPH: GraphSpec(pilot.GRAPH, pilot.GRAPH_VERSION, pilot.build, pilot.PilotInput, pilot.PilotDecision,
                           pilot.summary, pilot.EXPORT_FIELDS),
}


class AgentsDisabled(RuntimeError):
    """Agent graphs are off (AGENT_GRAPHS_ENABLED), or the database cannot hold durable checkpoints."""


class ThreadBusy(RuntimeError):
    """Another run holds this thread, or it is not in a state that allows the action."""


def enabled() -> bool:
    return get_settings().agent_graphs_enabled


def spec_for(name: str) -> GraphSpec:
    spec = GRAPHS.get(name)
    if spec is None:
        raise KeyError(f"unknown graph '{name}'")
    return spec


# ───────────── starting and claiming ─────────────

def start(db: Session, graph: str, payload: dict[str, Any], idempotency_key: str | None = None) -> AgentThread:
    """Create the account's thread (QUEUED), or return the one this idempotency key already made."""
    user_id = require_user_id(db)
    spec = spec_for(graph)
    clean = spec.input_model.model_validate(payload).model_dump()
    if safety.problem(clean):
        raise ValueError("graph input must not carry credentials")
    subject = threads.subject_for(user_id, idempotency_key)
    existing = db.scalar(select(AgentThread).where(AgentThread.graph == spec.name, AgentThread.subject == subject))
    if existing is not None:
        return threads.check(existing, user_id)
    thread = AgentThread(user_id=user_id, graph=spec.name, graph_version=spec.version, subject=subject,
                         thread_id=threads.thread_id_for(spec.name, user_id, subject, spec.version),
                         status="QUEUED", input=clean, trace_id=tracing.current_trace_id())
    db.add(thread)
    try:
        db.commit()
    except IntegrityError:  # the same key started twice at once: one thread
        db.rollback()
        existing = db.scalar(select(AgentThread).where(AgentThread.graph == spec.name,
                                                       AgentThread.subject == subject))
        return threads.check(existing, user_id)
    return thread


def claim(db: Session, thread_pk: int, user_id: int, statuses: tuple[str, ...], worker: str,
          now: datetime | None = None) -> RunHandle | None:
    """Take the lease of one of the account's threads in `statuses` whose lease is free or expired."""
    now = now or utcnow()
    token = str(uuid.uuid4())
    won = db.execute(
        update(AgentThread)
        .where(AgentThread.id == thread_pk, AgentThread.user_id == user_id, AgentThread.status.in_(statuses),
               or_(AgentThread.lock_expires_at.is_(None), AgentThread.lock_expires_at < now))
        .values(status="RUNNING", lock_token=token, locked_by=worker[:200], attempt_count=AgentThread.attempt_count + 1,
                lock_expires_at=now + timedelta(seconds=get_settings().agent_lease_seconds), started_at=now,
                updated_at=now)
        .execution_options(synchronize_session=False))
    db.commit()
    return RunHandle(thread_pk, user_id, token) if won.rowcount else None


def _write(sessions: Callable[[], Session], run: RunHandle, **values: Any) -> bool:
    """Update the thread only while this run holds it."""
    with sessions() as db:
        won = db.execute(update(AgentThread)
                         .where(AgentThread.id == run.thread_pk, AgentThread.user_id == run.user_id,
                                AgentThread.lock_token == run.token, AgentThread.status == "RUNNING")
                         .values(updated_at=utcnow(), **values).execution_options(synchronize_session=False))
        db.commit()
        return bool(won.rowcount)


@contextmanager
def _heartbeat(sessions: Callable[[], Session], run: RunHandle) -> Iterator[None]:
    """Renew the lease every third of its length while the run is in progress."""
    lease = get_settings().agent_lease_seconds
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(max(1.0, lease / 3)):
            try:
                if not _write(sessions, run, lock_expires_at=utcnow() + timedelta(seconds=lease)):
                    return
            except Exception:
                log.warning("could not renew agent thread lease", exc_info=True)

    worker = threading.Thread(target=beat, name=f"agent-lease-{run.thread_pk}", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()


# ───────────── executing ─────────────

@contextmanager
def _saver_for(sessions: Callable[[], Session], saver: Any | None) -> Iterator[Any]:
    if saver is not None:
        yield saver
        return
    with sessions() as db:
        engine = db.get_bind()
    if not checkpoint.durable(engine):
        raise AgentsDisabled("Agent graphs need PostgreSQL for durable checkpoints")
    with checkpoint.open_saver(engine) as opened:
        yield opened


def execute(sessions: Callable[[], Session], run: RunHandle, command: Any = None, *, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Run (or continue) a claimed thread until it ends or waits for a person. Returns the outcome; never raises.

    `command` is a LangGraph Command(resume=...) for a person's decision, or None to start or continue from the last
    checkpoint. `saver` is for tests (an in-memory saver); production opens the PostgreSQL checkpointer.
    """
    try:
        with sessions() as db:
            db.info["user_id"] = run.user_id
            thread = threads.owned(db, run.thread_pk)
            if thread.lock_token != run.token:
                return "lost"
            thread_id, graph, initial = thread.thread_id, thread.graph, thread.input
    except (threads.ThreadNotFound, threads.ThreadOwnershipError):
        return "refused"  # not this account's thread: the checkpointer is never opened
    spec = spec_for(graph)
    with tracing.trace(f"agent:{graph}", workflow_type="agent_graph", workflow_entity_id=run.thread_pk,
                       user_id=run.user_id) as ctx:
        _write(sessions, run, trace_id=ctx.trace_id if ctx else None)
        try:
            with (_saver_for(sessions, saver) as opened, _heartbeat(sessions, run),
                  tracing_boundary.export(spec.export_fields) as callbacks):
                app = spec.build(opened)
                config = {"configurable": {"thread_id": thread_id}, "run_name": f"agent:{graph}",
                          "callbacks": callbacks,
                          "metadata": {"graph": graph, "graph_version": spec.version,
                                       "trace_id": ctx.trace_id if ctx else None}}
                if command is None and opened.get_tuple({"configurable": {"thread_id": thread_id}}) is None:
                    argument: Any = dict(initial or {})  # first run: the stored input
                else:
                    argument = command  # resume a decision, or None: continue from the last checkpoint
                context = pilot.AgentContext(run=run, sessions=sessions, hold=hold)
                app.invoke(argument, config=config, context=context, durability="sync")
                state = app.get_state(config)
            return _settle(sessions, run, spec, state)
        except LeaseLost:
            return "lost"
        except Exception as exc:
            log.warning("agent graph %s thread %s failed", graph, run.thread_pk, exc_info=True)
            message = redact_text(f"{type(exc).__name__}: {exc}", ERROR_LIMIT)
            done = _write(sessions, run, status="FAILED", last_error=message, lock_token=None,
                          lock_expires_at=None, completed_at=utcnow())
            return "failed" if done else "lost"


def _settle(sessions: Callable[[], Session], run: RunHandle, spec: GraphSpec, state: Any) -> str:
    interrupts = getattr(state, "interrupts", ()) or ()
    if interrupts:
        value = interrupts[0].value
        shown = value if isinstance(value, dict) and not safety.problem(value) else {"kind": "input_required"}
        done = _write(sessions, run, status="INTERRUPTED", interrupt=shown, lock_token=None, lock_expires_at=None)
        return "interrupted" if done else "lost"
    if getattr(state, "next", ()):
        raise RuntimeError("graph stopped with pending steps")
    done = _write(sessions, run, status="COMPLETED", result=spec.summary(dict(state.values)), interrupt=None,
                  input=None, last_error=None, lock_token=None, lock_expires_at=None, completed_at=utcnow())
    return "completed" if done else "lost"


# ───────────── a person's actions ─────────────

def resume(db: Session, thread_pk: int, decision: dict[str, Any], worker: str) -> tuple[RunHandle, Any]:
    """Validate a decision for one of the account's waiting threads and take its lease. Returns (run, Command)."""
    from langgraph.types import Command

    user_id = require_user_id(db)
    thread = threads.owned(db, thread_pk)
    if thread.status != "INTERRUPTED":
        raise ThreadBusy(f"this run is {thread.status.lower()}, not waiting for a decision")
    clean = spec_for(thread.graph).decision_model.model_validate(decision).model_dump()
    run = claim(db, thread.id, user_id, ("INTERRUPTED",), worker)
    if run is None:
        raise ThreadBusy("another worker is resuming this run")
    _clear_interrupt(db, run)
    return run, Command(resume=clean)


def _clear_interrupt(db: Session, run: RunHandle) -> None:
    db.execute(update(AgentThread).where(AgentThread.id == run.thread_pk, AgentThread.lock_token == run.token)
               .values(interrupt=None).execution_options(synchronize_session=False))
    db.commit()


def stop(db: Session, thread_pk: int) -> AgentThread:
    """Stop one of the account's threads. A run in progress loses its lease and writes nothing more."""
    active = ("QUEUED", "RUNNING", "INTERRUPTED")
    thread = threads.owned(db, thread_pk)
    if thread.status not in active:
        raise ThreadBusy(f"this run is already {thread.status.lower()}")
    # Guarded on the status, so a run that finished meanwhile is never relabelled as stopped.
    now = utcnow()
    won = db.execute(update(AgentThread).where(AgentThread.id == thread.id, AgentThread.status.in_(active))
                     .values(status="STOPPED", last_error=STOPPED_BY_USER, completed_at=now, updated_at=now,
                             lock_token=None, lock_expires_at=None, interrupt=None)
                     .execution_options(synchronize_session=False)).rowcount
    db.commit()
    db.refresh(thread)
    if not won:
        raise ThreadBusy(f"this run is already {thread.status.lower()}")
    return thread


def public(thread: AgentThread) -> dict[str, Any]:
    """A thread as the API shows it: metadata only (no thread id, input or checkpoint)."""
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {"id": thread.id, "graph": thread.graph, "graph_version": thread.graph_version, "status": thread.status,
            "interrupt": thread.interrupt, "result": thread.result, "attempts": thread.attempt_count,
            "last_error": redact_text(thread.last_error, ERROR_LIMIT) if thread.last_error else None,
            "created_at": iso(thread.created_at), "updated_at": iso(thread.updated_at),
            "started_at": iso(thread.started_at), "completed_at": iso(thread.completed_at)}


# ───────────── the scheduler pass ─────────────

def claim_stale(sessions: Callable[[], Session], worker: str, limit: int, now: datetime | None = None
                ) -> list[RunHandle]:
    """Threads left QUEUED, or RUNNING with an expired lease (their worker died), up to `limit`. PostgreSQL."""
    now = now or utcnow()
    max_attempts = max(1, get_settings().agent_max_attempts)
    runs: list[RunHandle] = []
    with sessions() as db:
        rows = db.execute(
            select(AgentThread.id, AgentThread.user_id, AgentThread.attempt_count)
            .where(AgentThread.status.in_(("QUEUED", "RUNNING")),
                   or_(AgentThread.lock_expires_at.is_(None), AgentThread.lock_expires_at < now))
            .order_by(AgentThread.id).limit(max(1, limit)).with_for_update(skip_locked=True)).all()
        exhausted = [r.id for r in rows if r.attempt_count >= max_attempts]
        if exhausted:
            db.execute(update(AgentThread).where(AgentThread.id.in_(exhausted))
                       .values(status="FAILED", last_error=f"Stopped after {max_attempts} attempts",
                               lock_token=None, lock_expires_at=None, completed_at=now, updated_at=now)
                       .execution_options(synchronize_session=False))
        db.commit()
        for row in rows:
            if row.id in exhausted:
                continue
            run = claim(db, row.id, row.user_id, ("QUEUED", "RUNNING"), worker, now)
            if run is not None:
                runs.append(run)
    return runs


def prune(sessions: Callable[[], Session], now: datetime | None = None, limit: int = 200) -> dict[str, Any]:
    """Checkpoint retention: expire threads left waiting, delete checkpoints of finished and orphaned threads.

    Reads and writes of agent_threads are committed before the checkpointer is opened: LangGraph's setup must never
    wait on a transaction of ours."""
    settings = get_settings()
    if not settings.agent_graphs_enabled:
        return {"skipped": "Agent graphs are off"}
    now = now or utcnow()
    with sessions() as db:
        engine = db.get_bind()
        if not checkpoint.durable(engine):
            return {"skipped": "Agent graphs need PostgreSQL"}
        expired = db.execute(
            update(AgentThread)
            .where(AgentThread.status == "INTERRUPTED",
                   AgentThread.updated_at < now - timedelta(days=max(1, settings.agent_interrupt_expiry_days)))
            .values(status="STOPPED", last_error=EXPIRED_NOTE, interrupt=None, completed_at=now, updated_at=now)
            .execution_options(synchronize_session=False)).rowcount
        cutoff = now - timedelta(days=max(0, settings.agent_checkpoint_retention_days))
        finished = [(t.id, t.thread_id) for t in db.scalars(
            select(AgentThread)
            .where(AgentThread.status.in_(("COMPLETED", "FAILED", "STOPPED")),
                   AgentThread.checkpoint_pruned_at.is_(None),
                   and_(AgentThread.completed_at.is_not(None), AgentThread.completed_at < cutoff))
            .order_by(AgentThread.id).limit(limit))
            if threads.owner_in(t.thread_id) == t.user_id]
        db.commit()
    with checkpoint.open_saver(engine) as saver:
        for _, thread_id in finished:
            saver.delete_thread(thread_id)
        stored = checkpoint.thread_ids(saver, limit)
    known: set[str] = set()
    with sessions() as db:
        if finished:
            db.execute(update(AgentThread).where(AgentThread.id.in_([pk for pk, _ in finished]))
                       .values(checkpoint_pruned_at=now).execution_options(synchronize_session=False))
        if stored:
            known = set(db.scalars(select(AgentThread.thread_id).where(AgentThread.thread_id.in_(stored))))
        db.commit()
    orphans = [t for t in stored if t not in known]  # their account (and its agent_threads rows) is gone
    if orphans:
        with checkpoint.open_saver(engine) as saver:
            for thread_id in orphans:
                saver.delete_thread(thread_id)
    return {"expired": expired, "pruned": len(finished), "orphans": len(orphans)}
