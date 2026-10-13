"""Side effects a graph makes, made at most once.

LangGraph replays a node whose checkpoint was not written (a crash between the effect and the checkpoint), so a node
must never just "do" something. `once` records the effect under a key unique per thread, in the same transaction as a
check that this run still holds the thread's lease:

    with sessions() as db:
        effects.once(db, run, "pilot.completed", {"items": 3})

A replay finds the first record and returns it; a run that lost its lease (stopped, or taken over after its lease
expired) raises LeaseLost and writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import safety
from app.models import AgentEffect, AgentThread


class LeaseLost(RuntimeError):
    """This run no longer holds the thread: it must stop without writing."""


@dataclass(frozen=True)
class RunHandle:
    """Who is running a thread: its row, owner and lease token. Passed to nodes through the graph's config."""

    thread_pk: int
    user_id: int
    token: str


def _locked(db: Session, run: RunHandle) -> AgentThread:
    query = select(AgentThread).where(AgentThread.id == run.thread_pk, AgentThread.user_id == run.user_id)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    thread = db.scalar(query)
    if thread is None or thread.lock_token != run.token or thread.status != "RUNNING":
        raise LeaseLost("this run no longer holds the graph thread")
    return thread


def once(db: Session, run: RunHandle, key: str, summary: dict[str, Any] | None = None) -> tuple[AgentEffect, bool]:
    """Record the effect `key` for this thread once. Returns (record, created). Commits."""
    if safety.problem(summary):
        raise ValueError("an effect summary must not carry credentials")
    _locked(db, run)
    existing = db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == run.thread_pk,
                                                   AgentEffect.effect_key == key))
    if existing is not None:
        db.commit()
        return existing, False
    record = AgentEffect(user_id=run.user_id, agent_thread_id=run.thread_pk, effect_key=key[:120], summary=summary)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:  # a concurrent replay won the insert: the effect exists once
        db.rollback()
        record = db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == run.thread_pk,
                                                     AgentEffect.effect_key == key))
        return record, False
    return record, True
