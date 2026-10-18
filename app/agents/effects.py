"""Side effects a graph makes, made at most once.

LangGraph replays a node whose checkpoint was not written (a crash between the effect and the checkpoint), so a node
must never just "do" something. `once` records the effect under a key unique per thread, in the same transaction as a
check that this run still holds the thread's lease:

    with sessions() as db:
        effects.once(db, run, "pilot.completed", {"items": 3})

A replay finds the first record and returns it; a run that lost its lease (stopped, or taken over after its lease
expired) raises LeaseLost and writes nothing.

`within` is the same record made inside the caller's transaction, for an effect that *is* a business write (a node
queueing a recovery or ranking task through its service): the lease check (a row lock on PostgreSQL, held until the
caller commits), the record and the write commit together or not at all, so a stopped run can never queue work and a
replayed node finds the record and does not queue it twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import safety
from app.models import AgentEffect, AgentThread

KEY_LIMIT = 120  # agent_effects.effect_key


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


def _check(key: str, summary: dict[str, Any] | None) -> None:
    if not key or len(key) > KEY_LIMIT:
        raise ValueError(f"an effect key is 1 to {KEY_LIMIT} characters")  # never truncated: two keys must not merge
    if safety.problem(summary):
        raise ValueError("an effect summary must not carry credentials")


def within(db: Session, run: RunHandle, key: str, summary: dict[str, Any] | None = None) -> tuple[AgentEffect, bool]:
    """Check the lease and record `key` in the caller's transaction. Returns (record, created). Does not commit: the
    caller commits it together with the write it stands for (a concurrent duplicate fails that commit)."""
    _check(key, summary)
    _locked(db, run)
    existing = db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == run.thread_pk,
                                                   AgentEffect.effect_key == key))
    if existing is not None:
        return existing, False
    record = AgentEffect(user_id=run.user_id, agent_thread_id=run.thread_pk, effect_key=key, summary=summary)
    db.add(record)
    db.flush()
    return record, True


def held(db: Session, run: RunHandle) -> None:
    """Raise LeaseLost unless this run still holds its thread (a row lock on PostgreSQL until the caller commits)."""
    _locked(db, run)


def once(db: Session, run: RunHandle, key: str, summary: dict[str, Any] | None = None) -> tuple[AgentEffect, bool]:
    """Record the effect `key` for this thread once. Returns (record, created). Commits."""
    _check(key, summary)
    _locked(db, run)
    existing = db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == run.thread_pk,
                                                   AgentEffect.effect_key == key))
    if existing is not None:
        db.commit()
        return existing, False
    record = AgentEffect(user_id=run.user_id, agent_thread_id=run.thread_pk, effect_key=key, summary=summary)
    db.add(record)
    try:
        db.commit()
    except IntegrityError:  # a concurrent replay won the insert: the effect exists once
        db.rollback()
        record = db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == run.thread_pk,
                                                     AgentEffect.effect_key == key))
        return record, False
    return record, True
