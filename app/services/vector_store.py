"""Where embedding vectors are kept: `rag_vectors`, one row per index entry (app.models.RagIndexEntry).

    PostgreSQL with pgvector   embedding <schema>.vector (no fixed size: each row carries its `dimensions`, and a
                               search only ever compares rows of one index version and one size)
    SQLite (tests, local dev)  embedding as JSON text; similarity in Python
    PostgreSQL without pgvector  no table: `available()` is False and retrieval stays full-text

The table is created here (`setup`), not by Alembic — like LangGraph's checkpoint tables — because whether it can exist
depends on the server having the extension, and an install without it must still start. Alembic ignores it
(app/migrations/env.py). Deleting an entry, an index version or the account deletes its vectors (ON DELETE CASCADE on
PostgreSQL; `delete_entries` does it explicitly on SQLite, which does not enforce foreign keys).

There is no approximate (ANN) index on purpose: a search is always restricted first to the few hundred entries one
job may use (the account, the index version and the evidence of the job's own resume), and an exact scan over those
is both correct and fast. Add one only when a measured account outgrows that.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from typing import Any

from sqlalchemy import DDL, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.db import Base

log = logging.getLogger("jobhunt.vector_store")
TABLE = "rag_vectors"

# The vector table references the index tables but is not in the ORM metadata, so a metadata-wide drop (tests, tools)
# must remove it first or PostgreSQL refuses to drop rag_index_entries.
event.listen(Base.metadata, "before_drop", DDL(f"DROP TABLE IF EXISTS {TABLE}"))
event.listen(Base.metadata, "after_drop", lambda *_args, **_kwargs: reset())

_lock = threading.Lock()
# engine url → (available, extension schema or None, reason)
_state: dict[str, tuple[bool, str | None, str | None]] = {}


def _key(bind: Engine | Connection) -> str:
    # Per engine, not per URL: two engines on one database may use different schemas (search_path).
    engine = bind.engine if isinstance(bind, Connection) else bind
    return f"{id(engine)}:{engine.url.render_as_string(hide_password=True)}"


def reset() -> None:
    """Forget what setup() found (tests that drop the schema)."""
    with _lock:
        _state.clear()


def _pg_setup(conn: Connection) -> tuple[bool, str | None, str | None]:
    schema = conn.execute(text("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
                               "WHERE e.extname = 'vector'")).scalar()
    if schema is None:
        if not conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar():
            return False, None, "VECTOR_UNAVAILABLE: the pgvector extension is not installed on this PostgreSQL"
        try:
            with conn.begin_nested():
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"))
        except Exception as exc:  # noqa: BLE001  no privilege to create it: run without vectors
            return False, None, f"VECTOR_UNAVAILABLE: could not create the vector extension ({type(exc).__name__})"
        schema = "public"
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            entry_id INTEGER PRIMARY KEY REFERENCES rag_index_entries(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            index_version_id INTEGER NOT NULL REFERENCES rag_index_versions(id) ON DELETE CASCADE,
            dimensions INTEGER NOT NULL CHECK (dimensions > 0),
            content_hash VARCHAR(64) NOT NULL,
            embedding "{schema}".vector NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
        )"""))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_scope ON {TABLE} (user_id, index_version_id)"))
    return True, schema, None


def _sqlite_setup(conn: Connection) -> tuple[bool, str | None, str | None]:
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            entry_id INTEGER PRIMARY KEY REFERENCES rag_index_entries(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            index_version_id INTEGER NOT NULL,
            dimensions INTEGER NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            embedding TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_scope ON {TABLE} (user_id, index_version_id)"))
    return True, None, None


def setup(bind: Engine | Connection) -> tuple[bool, str | None]:
    """Make sure the vector table exists. (available, reason when not). Idempotent and never raises."""
    key = _key(bind)
    with _lock:
        if key in _state and _state[key][0]:
            return True, None
    try:
        if isinstance(bind, Connection):
            found = _pg_setup(bind) if bind.dialect.name == "postgresql" else _sqlite_setup(bind)
        else:
            with bind.begin() as conn:
                found = _pg_setup(conn) if conn.dialect.name == "postgresql" else _sqlite_setup(conn)
    except Exception as exc:  # retrieval falls back to full-text; the reason is reported
        log.warning("vector store setup failed", exc_info=True)
        found = (False, None, f"VECTOR_UNAVAILABLE: {type(exc).__name__}")
    with _lock:
        _state[key] = found
    return found[0], found[2]


def _schema(db: Session) -> str | None:
    return _state.get(_key(db.get_bind()), (False, None, None))[1]


def available(db: Session) -> bool:
    """Whether vectors can be stored and searched here (runs setup the first time)."""
    key = _key(db.get_bind())
    hit = _state.get(key)
    if hit is None:
        return setup(db.get_bind())[0]
    return hit[0]


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def upsert(db: Session, rows: list[dict[str, Any]]) -> None:
    """rows: {entry_id, user_id, index_version_id, dimensions, content_hash, embedding}."""
    if not rows:
        return
    if db.get_bind().dialect.name == "postgresql":
        schema = _schema(db) or "public"
        db.execute(text(f"""
            INSERT INTO {TABLE} (entry_id, user_id, index_version_id, dimensions, content_hash, embedding)
            VALUES (:entry_id, :user_id, :index_version_id, :dimensions, :content_hash,
                    CAST(:embedding AS "{schema}".vector))
            ON CONFLICT (entry_id) DO UPDATE SET dimensions = EXCLUDED.dimensions,
                content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding,
                index_version_id = EXCLUDED.index_version_id"""),
            [{**r, "embedding": _literal(r["embedding"])} for r in rows])
    else:
        db.execute(text(f"""
            INSERT OR REPLACE INTO {TABLE} (entry_id, user_id, index_version_id, dimensions, content_hash, embedding)
            VALUES (:entry_id, :user_id, :index_version_id, :dimensions, :content_hash, :embedding)"""),
            [{**r, "embedding": json.dumps(r["embedding"])} for r in rows])


def stored_hashes(db: Session, user_id: int, entry_ids: list[int]) -> dict[int, str]:
    """entry id → hash of the text its stored vector was made from."""
    if not entry_ids or not available(db):
        return {}
    out: dict[int, str] = {}
    for chunk in _chunks(entry_ids):
        rows = db.execute(text(f"SELECT entry_id, content_hash FROM {TABLE} WHERE user_id = :u AND entry_id IN "
                               f"({_marks(chunk)})"), {"u": user_id, **_binds(chunk)}).all()
        out.update({int(r[0]): r[1] for r in rows})
    return out


def delete_entries(conn: Connection | Session, entry_ids: list[int]) -> None:
    """Remove the vectors of these entries (SQLite has no cascade). Silently nothing when there is no table."""
    if not entry_ids:
        return
    bind = conn.get_bind() if isinstance(conn, Session) else conn
    if not _state.get(_key(bind), (False,))[0]:
        return
    for chunk in _chunks(entry_ids):
        conn.execute(text(f"DELETE FROM {TABLE} WHERE entry_id IN ({_marks(chunk)})"), _binds(chunk))


def search(db: Session, *, user_id: int, index_version_id: int, dimensions: int, query: list[float],
           entry_hashes: dict[int, str], limit: int) -> list[tuple[int, float]]:
    """[(entry id, cosine similarity)] best first, over `entry_hashes` only (the entries this request may use) and
    only where the stored vector was made from the live text (`content_hash` equal): a stale vector is never used."""
    if not entry_hashes or not available(db) or len(query) != dimensions:
        return []
    ids = list(entry_hashes)
    hits: list[tuple[int, float]] = []
    if db.get_bind().dialect.name == "postgresql":
        schema = _schema(db) or "public"
        for chunk in _chunks(ids):
            rows = db.execute(text(f"""
                SELECT entry_id, content_hash,
                       1 - (embedding OPERATOR("{schema}".<=>) CAST(:q AS "{schema}".vector)) AS similarity
                FROM {TABLE}
                WHERE user_id = :u AND index_version_id = :v AND dimensions = :d AND entry_id IN ({_marks(chunk)})"""),
                {"q": _literal(query), "u": user_id, "v": index_version_id, "d": dimensions, **_binds(chunk)}).all()
            hits += [(int(r[0]), float(r[2])) for r in rows if entry_hashes.get(int(r[0])) == r[1]
                     and r[2] is not None and math.isfinite(float(r[2]))]
    else:
        qnorm = math.sqrt(sum(x * x for x in query)) or 1.0
        for chunk in _chunks(ids):
            rows = db.execute(text(f"""
                SELECT entry_id, content_hash, embedding FROM {TABLE}
                WHERE user_id = :u AND index_version_id = :v AND dimensions = :d AND entry_id IN ({_marks(chunk)})"""),
                {"u": user_id, "v": index_version_id, "d": dimensions, **_binds(chunk)}).all()
            for entry_id, content_hash, raw in rows:
                if entry_hashes.get(int(entry_id)) != content_hash:
                    continue
                vector = json.loads(raw)
                if len(vector) != dimensions:
                    continue
                vnorm = math.sqrt(sum(x * x for x in vector)) or 1.0
                hits.append((int(entry_id), sum(a * b for a, b in zip(query, vector, strict=True)) / (qnorm * vnorm)))
    hits.sort(key=lambda h: (-h[1], h[0]))
    return hits[:max(0, limit)]


def counts(db: Session, user_id: int) -> dict[int, int]:
    """index version id → stored vectors, for status pages."""
    if not available(db):
        return {}
    rows = db.execute(text(f"SELECT index_version_id, COUNT(*) FROM {TABLE} WHERE user_id = :u "
                           "GROUP BY index_version_id"), {"u": user_id}).all()
    return {int(r[0]): int(r[1]) for r in rows}


CHUNK = 500


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]


def _marks(chunk: list[int]) -> str:
    return ", ".join(f":id{i}" for i in range(len(chunk)))


def _binds(chunk: list[int]) -> dict[str, int]:
    return {f"id{i}": int(v) for i, v in enumerate(chunk)}
