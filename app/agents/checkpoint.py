"""The durable LangGraph checkpointer: PostgresSaver on the application's own database, behind a guard.

LangGraph owns its checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`) and migrates them itself in `setup()`; Alembic ignores them (app/migrations/env.py).
`GuardedSerializer` refuses, before anything is written, a checkpoint that is larger than
AGENT_CHECKPOINT_MAX_BYTES or that holds a credential field or a secret-shaped string (app.agents.safety): a
checkpoint is small, resumable computation state (ids, enums, counts), never a document, a page or a token.

A saver uses one dedicated psycopg connection, taken out of the SQLAlchemy pool (so it has the engine's options,
e.g. a test schema's search_path) and closed when the run ends. SQLite has no durable saver: graphs need PostgreSQL.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.engine import Engine

from app.agents import safety
from app.settings import get_settings

# Arbitrary constant: a PostgreSQL advisory lock so two processes never run LangGraph's table setup at once.
SETUP_LOCK_KEY = 7_220_310_044
SETUP_LOCK_TIMEOUT_SECONDS = 15
_ready: set[str] = set()
LIBRARY_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


class CheckpointRejected(ValueError):
    """A checkpoint that must not be stored: too large, or carrying something secret."""


class GuardedSerializer(JsonPlusSerializer):
    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        found = safety.problem(obj)
        if found:
            raise CheckpointRejected(f"checkpoint refused: {found}")
        kind, data = super().dumps_typed(obj)
        limit = get_settings().agent_checkpoint_max_bytes
        if data is not None and len(data) > limit:
            raise CheckpointRejected(f"checkpoint refused: {len(data)} bytes is over the {limit}-byte limit")
        if data and safety.secret_shaped(data.decode("utf-8", "ignore")):
            raise CheckpointRejected("checkpoint refused: secret-shaped text")
        return kind, data


def durable(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


@contextmanager
def _connection(engine: Engine) -> Iterator[Any]:
    from psycopg.rows import dict_row

    pooled = engine.raw_connection()
    conn = pooled.driver_connection
    pooled.detach()  # ours alone from now on: autocommit and the row factory never leak back into the pool
    try:
        conn.rollback()
        conn.autocommit = True
        conn.row_factory = dict_row
        yield conn
    finally:
        with suppress(Exception):  # closing a broken connection must not hide the run's own error
            conn.close()


def _set_up(engine: Engine, saver: Any, conn: Any) -> None:
    """Run LangGraph's own table migrations once per process and database (idempotent, one process at a time).

    They build indexes CONCURRENTLY, which waits for every open transaction: the lock timeout turns a long wait into
    a failed run (retried by the recovery pass) instead of a hung one. Callers never hold a transaction meanwhile.
    """
    schema = conn.execute("SELECT current_schema() AS schema").fetchone()["schema"]
    key = f"{engine.url.render_as_string(hide_password=True)}#{schema}"  # the same URL may serve several schemas
    if key in _ready:
        return
    conn.execute("SELECT pg_advisory_lock(%s)", (SETUP_LOCK_KEY,))
    try:
        conn.execute(f"SET lock_timeout = '{int(SETUP_LOCK_TIMEOUT_SECONDS)}s'")
        saver.setup()
        conn.execute("RESET lock_timeout")
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (SETUP_LOCK_KEY,))
    _ready.add(key)


def forget_setup() -> None:
    """Tests that drop the schema: run LangGraph's setup again on next use."""
    _ready.clear()


@contextmanager
def open_saver(engine: Engine) -> Iterator[Any]:
    """A PostgresSaver for one run, with LangGraph's tables set up."""
    from langgraph.checkpoint.postgres import PostgresSaver

    if not durable(engine):
        raise RuntimeError("Agent graphs need PostgreSQL for durable checkpoints")
    with _connection(engine) as conn:
        saver = PostgresSaver(conn, serde=GuardedSerializer())
        _set_up(engine, saver, conn)
        yield saver


def thread_ids(saver: Any, limit: int = 500) -> list[str]:
    """Thread ids that have checkpoints (for orphan cleanup); bounded."""
    with saver._cursor() as cur:
        rows = cur.execute("SELECT DISTINCT thread_id FROM checkpoints LIMIT %s", (limit,)).fetchall()
    return [row["thread_id"] for row in rows]
