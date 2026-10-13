"""Graph thread identity and ownership.

A thread id is built here, from the graph, the owner and a subject, and never taken from a caller: an API client
names a run by its `agent_threads.id`, which is looked up through the account's own session (another account's row
does not exist for it). `owned` also checks the owner written into the thread id itself, so even a row reached some
other way cannot hand one account's checkpoints to another. Every checkpointer access goes through `owned` first.
"""
from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentThread
from app.tenancy import require_user_id

_THREAD_ID = re.compile(r"^(?P<graph>[a-z][a-z0-9_]{0,40}):u(?P<user>\d+):(?P<subject>[0-9a-f]{8,64}):(?P<version>v\d+)$")


class ThreadNotFound(LookupError):
    """No such thread for this account (also what another account's thread looks like)."""


class ThreadOwnershipError(PermissionError):
    """A thread id that does not belong to the account asking for it."""


def subject_for(user_id: int, idempotency_key: str | None) -> str:
    """A fresh subject, or the same one for the same key, so repeating a start returns the first thread."""
    if not idempotency_key:
        return uuid.uuid4().hex
    return hashlib.sha256(f"{user_id}|{idempotency_key}".encode()).hexdigest()[:32]


def thread_id_for(graph: str, user_id: int, subject: str, version: str) -> str:
    thread_id = f"{graph}:u{int(user_id)}:{subject}:{version}"
    if not _THREAD_ID.match(thread_id):
        raise ValueError("invalid graph thread identity")
    return thread_id


def owner_in(thread_id: str) -> int | None:
    match = _THREAD_ID.match(thread_id or "")
    return int(match["user"]) if match else None


def check(thread: AgentThread, user_id: int) -> AgentThread:
    """The second check: the row and the thread id both belong to `user_id`."""
    if thread.user_id != user_id or owner_in(thread.thread_id) != user_id:
        raise ThreadOwnershipError("this graph thread belongs to another account")
    return thread


def owned(db: Session, thread_pk: int) -> AgentThread:
    """The account's own thread by id, or ThreadNotFound. The session must belong to the account."""
    user_id = require_user_id(db)
    thread = db.get(AgentThread, thread_pk)
    if thread is None:
        raise ThreadNotFound(thread_pk)
    return check(thread, user_id)


def listed(db: Session, limit: int = 50) -> list[AgentThread]:
    """The account's own threads, newest first. Reads agent_threads only, never the checkpointer."""
    require_user_id(db)
    return list(db.scalars(select(AgentThread).order_by(AgentThread.id.desc()).limit(max(1, min(limit, 200)))))
