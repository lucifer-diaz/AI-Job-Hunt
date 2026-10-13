"""Helpers for the agent graph tests (app.agents)."""
from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agents import checkpoint, runtime
from app.models import AgentEffect, AgentThread

# Synthetic credentials: shaped like real ones, valid nowhere. SHAPED ones are recognisable anywhere in text;
# the rest (a password, an OTP, a cookie value) only by the field that holds them.
SECRETS = {
    "openai": "sk-proj-SYNTHETICabcdefghijklmnop123456",
    "anthropic": "sk-ant-SYNTHETICabcdefghijklmnop123456",
    "google_access": "ya29.SYNTHETICabcdefghijklmnopqrstu",
    "google_refresh": "1//0SYNTHETICabcdefghijklmnopqrstuvwxyz",
    "google_client_secret": "GOCSPX-SYNTHETICabcdefghij",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGljc2lnbmF0dXJl",
    "bearer": "Bearer SYNTHETICbearertoken12345",
    "password": "hunter2-SYNTHETIC-password",
    "otp": "482915",
    "cookie": "sessionid=SYNTHETICcookievalue12345",
}
SHAPED = ("openai", "anthropic", "google_access", "google_refresh", "google_client_secret", "jwt", "bearer")


def memory_saver() -> InMemorySaver:
    return InMemorySaver(serde=checkpoint.GuardedSerializer())


def as_user(sessions, user_id: int):
    db = sessions()
    db.info["user_id"] = user_id
    return db


def start(sessions, user_id: int, payload: dict[str, Any] | None = None, key: str | None = None) -> int:
    with as_user(sessions, user_id) as db:
        return runtime.start(db, "pilot", payload or {"kind": "auto", "items": 3}, key).id


def claim(sessions, user_id: int, thread_pk: int, statuses=("QUEUED",), worker="test") -> runtime.RunHandle | None:
    with as_user(sessions, user_id) as db:
        return runtime.claim(db, thread_pk, user_id, statuses, worker)


def thread(sessions, thread_pk: int) -> AgentThread:
    with sessions() as db:
        row = db.get(AgentThread, thread_pk)
        db.expunge(row)
        return row


def effects(sessions, thread_pk: int) -> list[AgentEffect]:
    with sessions() as db:
        return list(db.query(AgentEffect).filter(AgentEffect.agent_thread_id == thread_pk))


def resume(sessions, user_id: int, thread_pk: int, decision: str, worker="test"):
    with as_user(sessions, user_id) as db:
        return runtime.resume(db, thread_pk, {"decision": decision}, worker)
