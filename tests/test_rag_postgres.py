"""Retrieval on PostgreSQL with pgvector: the vector table, cosine search through the extension's operator, full-text
ranking, account deletion removing every derived row, tenant isolation in raw SQL, and concurrent claims.

Needs TEST_DATABASE_URL (skipped otherwise) on a server where the pgvector extension is installed or installable."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.db import AppSession
from app.services import rag_index, rag_retrieval, vector_store
from app.settings import get_settings
from tests import rag_support as rs
from tests.conftest import make_user


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    vector_store.reset()  # other tests drop and re-create the schema
    rag_retrieval.clear_cache()
    rs.use_concepts(monkeypatch)
    factory = sessionmaker(bind=pg_engine, class_=AppSession, autoflush=False)
    available, reason = vector_store.setup(pg_engine)
    if not available:
        pytest.skip(f"pgvector is not available on this PostgreSQL: {reason}")
    return factory


def build(sessions):
    data = rs.load(sessions, make_user(sessions))
    summary = rag_index.run_account(sessions, data.user_id, service=rs.concept_service(data.user_id))
    assert summary["status"] == "SERVING", summary
    return data


def pack(sessions, data, key, **kwargs):
    with rs.session_for(sessions, data.user_id) as db:
        profile, job = rs.profile_and_job(db, data.jobs[key])
        return rag_retrieval.retrieve_evidence(db, profile, job, **kwargs)


def count(sessions, table: str, user_id: int) -> int:
    with sessions() as db:
        return db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE user_id = :u"), {"u": user_id}).scalar()


def test_vectors_live_in_a_pgvector_column(sessions, pg_engine):
    data = build(sessions)
    with pg_engine.connect() as conn:
        kind = conn.execute(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                                 "WHERE attrelid = 'rag_vectors'::regclass AND attname = 'embedding'")).scalar()
    assert kind == "vector" or kind.endswith(".vector")
    assert count(sessions, "rag_vectors", data.user_id) == len(data.facts)


def test_hybrid_retrieval_uses_pgvector_and_full_text_and_keeps_the_pin(sessions):
    data = build(sessions)
    for case in data.cases:
        result = pack(sessions, data, case.key)
        assert result.mode == "hybrid" and not set(result.evidence_ids) & case.forbidden, case.key
    conflict = pack(sessions, data, "data_kafka_conflict")
    assert conflict.evidence_ids[0] == data.facts["kafka_feature_store"]
    assert any("fulltext" in item.scores for item in pack(sessions, data, "ml_churn").items)
    assert any("vector" in item.scores for item in conflict.items)


def test_deleting_an_account_deletes_every_derived_row(sessions):
    data = build(sessions)
    keep = build(sessions)
    with sessions() as db:
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": data.user_id})
        db.commit()
    for table in ("evidence_resume_links", "rag_index_versions", "rag_index_entries", "rag_vectors"):
        assert count(sessions, table, data.user_id) == 0, table
    assert count(sessions, "rag_vectors", keep.user_id) == len(keep.facts)


def test_raw_vector_search_never_crosses_accounts(sessions):
    data = build(sessions)
    other = build(sessions)
    with rs.session_for(sessions, data.user_id) as db:
        version = rag_index.serving_version(db, data.user_id)
        theirs = {row[0]: row[1] for row in db.execute(text(
            "SELECT id, content_hash FROM rag_index_entries WHERE user_id = :u"), {"u": other.user_id})}
        query = [0.1] * version.dimensions
        assert vector_store.search(db, user_id=data.user_id, index_version_id=version.id,
                                   dimensions=version.dimensions, query=query, entry_hashes=theirs, limit=50) == []
    mine = set(data.facts.values())
    for case in data.cases:
        assert set(pack(sessions, data, case.key).evidence_ids) <= mine


def test_concurrent_claims_never_take_the_same_entry(sessions):
    data = rs.load(sessions, make_user(sessions))
    with rs.session_for(sessions, data.user_id) as db:
        version = rag_index.ensure_version(db, data.user_id, rs.CONCEPT_MODEL)
        rag_index.sweep(db, data.user_id, version)
        db.commit()
        version_id = version.id
    first, second = rs.session_for(sessions, data.user_id), rs.session_for(sessions, data.user_id)
    try:
        a = rag_index.claim(first, data.user_id, version_id, 6, at=rag_index.now())
        b = rag_index.claim(second, data.user_id, version_id, 6, at=rag_index.now())
    finally:
        first.close()
        second.close()
    taken_a, taken_b = {e[0] for e in a.entries}, {e[0] for e in b.entries}
    assert taken_a and taken_b and not taken_a & taken_b
