"""Agent graph API: start, list, read, resume and stop the account's own graph runs (app.agents).

Every endpoint answers 404 unless AGENT_GRAPHS_ENABLED. A run is named by its `agent_threads` id, read through the
account's session, so another account's run does not exist here. Runs execute as background tasks; nothing here
waits for a graph. Responses are metadata only: no thread id, input, checkpoint or trace payload.
"""
from __future__ import annotations

import os
import socket
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents import runtime, threads
from app.auth import CurrentUser, current_user, get_user_db

router = APIRouter()


def _sessions():
    from app.db import SessionLocal

    return SessionLocal


def _worker() -> str:
    return f"api:{socket.gethostname()}:{os.getpid()}"


def _require_enabled(db: Session) -> None:
    if not runtime.enabled():
        raise HTTPException(404, "Not found")
    if db.get_bind().dialect.name != "postgresql":
        raise HTTPException(503, "Agent graphs need PostgreSQL for durable checkpoints")


class PilotStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["auto", "review"] = "auto"
    items: int = Field(default=3, ge=1, le=20)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=80)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]


def _dispatch(background: BackgroundTasks, db: Session, thread_pk: int, user_id: int,
              statuses: tuple[str, ...] = ("QUEUED",), command: Any = None) -> None:
    run = runtime.claim(db, thread_pk, user_id, statuses, _worker())
    if run is not None:  # otherwise the recovery pass (or another request) already has it
        background.add_task(runtime.execute, _sessions(), run, command)


def _shown(db: Session, thread: Any) -> dict[str, Any]:
    """The run as the API shows it, with the request's transaction ended: a background run opens the checkpointer,
    whose setup must not wait for this request."""
    db.refresh(thread)
    payload = runtime.public(thread)
    db.commit()
    return payload


def _owned(db: Session, run_id: int):
    try:
        return threads.owned(db, run_id)
    except (threads.ThreadNotFound, threads.ThreadOwnershipError):
        raise HTTPException(404, "No such run") from None


@router.post("/api/agents/pilot/runs", status_code=202)
def start_pilot(body: PilotStart, background: BackgroundTasks, user: CurrentUser = Depends(current_user),
                db: Session = Depends(get_user_db)):
    """Start the pilot graph (no production side effect). The same idempotency key returns the same run."""
    _require_enabled(db)
    thread = runtime.start(db, "pilot", {"kind": body.kind, "items": body.items}, body.idempotency_key)
    if thread.status == "QUEUED":
        _dispatch(background, db, thread.id, user.id)
    return _shown(db, thread)


@router.get("/api/agents/runs")
def list_runs(limit: int = 50, db: Session = Depends(get_user_db)):
    _require_enabled(db)
    runs = [runtime.public(t) for t in threads.listed(db, limit)]
    db.commit()
    return {"runs": runs}


@router.get("/api/agents/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_user_db)):
    _require_enabled(db)
    return _shown(db, _owned(db, run_id))


@router.post("/api/agents/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: int, body: Decision, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Answer a run that waits for a person. Ownership is checked again here, before the checkpoint is touched."""
    _require_enabled(db)
    _owned(db, run_id)
    try:
        run, command = runtime.resume(db, run_id, body.model_dump(), _worker())
    except runtime.ThreadBusy as exc:
        raise HTTPException(409, str(exc)) from None
    background.add_task(runtime.execute, _sessions(), run, command)
    return _shown(db, _owned(db, run_id))


@router.post("/api/agents/runs/{run_id}/stop")
def stop_run(run_id: int, db: Session = Depends(get_user_db)):
    _require_enabled(db)
    _owned(db, run_id)
    try:
        return _shown(db, runtime.stop(db, run_id))
    except runtime.ThreadBusy as exc:
        raise HTTPException(409, str(exc)) from None
