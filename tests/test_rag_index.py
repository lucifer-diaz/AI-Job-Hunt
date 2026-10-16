"""The derived embedding index: incremental reindex on edits, verification changes and deletions, lease-guarded
writes, versioned backfill on a model or vector-size change, retention, the scheduler job and the tag API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import (
    Evidence,
    EvidenceResumeLink,
    KnowledgeAnswer,
    RagIndexEntry,
    RagIndexVersion,
    Resume,
)
from app.services import rag_index, rag_retrieval, vector_store
from app.services.embeddings import EmbeddingError, EmbeddingModel, Embeddings, EmbeddingService
from app.settings import get_settings
from tests import rag_support as rs
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    monkeypatch.setattr(get_settings(), "rag_backfill_min_coverage", 1.0)
    rag_retrieval.clear_cache()
    rs.use_concepts(monkeypatch)


@pytest.fixture
def data():
    return rs.load(SessionLocal, make_user(SessionLocal))


class Spy:
    """A concept backend that records what it was asked to embed, and can fail or answer the wrong size."""

    def __init__(self, fail: str | None = None, size: int | None = None):
        self.inner, self.texts, self.fail, self.size = rs.ConceptBackend(), [], fail, size

    def embed(self, model: EmbeddingModel, texts: list[str], operation: str) -> Embeddings:
        self.texts += texts
        if self.fail:
            raise EmbeddingError(self.fail, "provider says no")
        out = self.inner.embed(model, texts, operation)
        if self.size:
            out.vectors = [v[:self.size] for v in out.vectors]
        return out


def run(data, backend=None, model=rs.CONCEPT_MODEL):
    service = EmbeddingService(model, backend or rs.ConceptBackend(), data.user_id)
    return rag_index.run_account(SessionLocal, data.user_id, service=service)


def entries(data, **where):
    with rs.session_for(SessionLocal, data.user_id) as db:
        query = select(RagIndexEntry).where(RagIndexEntry.user_id == data.user_id)
        for key, value in where.items():
            query = query.where(getattr(RagIndexEntry, key) == value)
        return list(db.scalars(query).all())


def vectors(data) -> int:
    with SessionLocal() as db:
        return db.execute(text("SELECT COUNT(*) FROM rag_vectors WHERE user_id = :u"), {"u": data.user_id}).scalar()


def test_a_first_build_covers_every_verified_row_and_serves(data):
    summary = run(data)
    assert summary["status"] == "SERVING" and summary["eligible"] == summary["covered"] == len(data.facts)
    assert summary["embedded"] == len(data.facts) and vectors(data) == len(data.facts)
    assert run(data)["embedded"] == 0  # nothing changed: no model call


def test_an_edit_is_reembedded_and_an_unverified_fact_keeps_no_vector(data):
    run(data)
    spy = Spy()
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.get(Evidence, data.facts["mentoring"]).claim = "Mentored 7 junior engineers"
        db.commit()
    assert [e.status for e in entries(data, source_id=data.facts["mentoring"])] == ["PENDING"]
    assert run(data, spy)["embedded"] == 1 and spy.texts == [
        "Mentored 7 junior engineers\nCompany: Acme\nSkills: mentoring, leadership"]
    with rs.session_for(SessionLocal, data.user_id) as db:  # un-verify, then verify again without an edit
        db.get(Evidence, data.facts["pycon_talk"]).verified = False
        db.commit()
    entry = entries(data, source_id=data.facts["pycon_talk"])[0]
    assert entry.status == "DEACTIVATED" and entry.content_hash is None
    with SessionLocal() as db:  # the withdrawn fact's vector is gone at once, not left for retention
        assert db.execute(text("SELECT COUNT(*) FROM rag_vectors WHERE entry_id = :e"), {"e": entry.id}).scalar() == 0
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.get(Evidence, data.facts["pycon_talk"]).verified = True
        db.commit()
    spy = Spy()
    summary = run(data, spy)
    assert summary["embedded"] == 1 and summary["reused"] == 0 and len(spy.texts) == 1


def test_the_sweep_removes_a_vector_left_behind_by_a_missed_unverify(data):
    run(data)
    with rs.session_for(SessionLocal, data.user_id) as db:  # a bulk UPDATE: no flush listener sees it
        db.execute(update(Evidence).where(Evidence.id == data.facts["pycon_talk"]).values(verified=False))
        db.commit()
    before = vectors(data)
    run(data)
    entry = entries(data, source_id=data.facts["pycon_talk"])[0]
    assert entry.status == "DEACTIVATED" and vectors(data) == before - 1


def test_a_deletion_removes_its_entries_vectors_and_tags(data):
    run(data)
    before = vectors(data)
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.delete(db.get(Evidence, data.facts["uplift_paper"]))
        db.commit()
        assert db.scalar(select(func.count()).select_from(EvidenceResumeLink).where(
            EvidenceResumeLink.evidence_id == data.facts["uplift_paper"])) == 0
    assert entries(data, source_id=data.facts["uplift_paper"], source_type="evidence") == []
    assert vectors(data) == before - 1


def test_a_deleted_resume_takes_its_tags_with_it(data):
    with rs.session_for(SessionLocal, data.user_id) as db:
        from app.services import resume_verification

        resume_verification.delete_version(db, db.get(Resume, data.resumes["data"]))
        db.commit()
        assert db.scalar(select(func.count()).select_from(EvidenceResumeLink).where(
            EvidenceResumeLink.resume_id == data.resumes["data"])) == 0


def test_the_sweep_repairs_a_change_the_listener_did_not_see(data):
    run(data)
    with SessionLocal() as db:  # a raw write: no ORM flush, so no listener
        db.execute(update(Evidence).where(Evidence.id == data.facts["oncall"]).values(claim="Ran on-call for 9"))
        db.commit()
    spy = Spy()
    assert run(data, spy)["embedded"] == 1 and spy.texts[0].startswith("Ran on-call for 9")


def test_a_sensitive_answer_is_embedded_by_its_question_only(data):
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.add(KnowledgeAnswer(question="Do you need visa sponsorship?", question_key="q-visa",
                               answer="Yes, an H-1B transfer", verified=True, sensitive=True))
        db.commit()
    spy = Spy()
    run(data, spy)
    visa = [t for t in spy.texts if "visa" in t]
    assert visa == ["Question: Do you need visa sponsorship?"] and not any("H-1B" in t for t in spy.texts)


def test_an_edit_while_embedding_never_stores_the_old_texts_vector(data):
    with rs.session_for(SessionLocal, data.user_id) as db:
        version = rag_index.ensure_version(db, data.user_id, rs.CONCEPT_MODEL)
        rag_index.sweep(db, data.user_id, version)
        db.commit()
        version_id = version.id
    vector_store.setup(SessionLocal().get_bind())
    with rs.session_for(SessionLocal, data.user_id) as db:
        claimed = rag_index.claim(db, data.user_id, version_id, 100, at=rag_index.now())

    class EditsMidway(rs.ConceptBackend):
        def embed(self, model, texts, operation):
            with rs.session_for(SessionLocal, data.user_id) as db:  # the person edits while the model works
                db.get(Evidence, data.facts["churn"]).claim = "Predicted churn with gradient boosting"
                db.commit()
            return super().embed(model, texts, operation)

    service = EmbeddingService(rs.CONCEPT_MODEL, EditsMidway(), data.user_id)
    out = rag_index.process(SessionLocal, data.user_id, service, version_id, 64, claimed)
    assert out["lost_lease"] == 1
    [entry] = entries(data, source_id=data.facts["churn"], source_type="evidence")
    assert entry.status == "PENDING" and entry.content_hash is None


def test_a_provider_failure_keeps_the_entries_for_a_later_retry(data):
    summary = run(data, Spy(fail="UNAVAILABLE"))
    assert summary["error"].startswith("UNAVAILABLE") and summary["status"] == "BUILDING"
    waiting = entries(data, status="PENDING")  # retrying (Running workers): attempts are left
    assert waiting and all(e.next_attempt_at is not None and e.attempt_count == 1 and e.last_error for e in waiting)
    assert not entries(data, status="FAILED")
    with rs.session_for(SessionLocal, data.user_id) as db:  # the backoff passes
        db.execute(update(RagIndexEntry).where(RagIndexEntry.user_id == data.user_id)
                   .values(next_attempt_at=None))
        db.commit()
    assert run(data)["status"] == "SERVING"


def test_a_wrong_vector_size_stores_nothing(data):
    summary = run(data, Spy(size=10))
    assert summary["error"].startswith("DIMENSION_MISMATCH") and vectors(data) == 0


def test_a_new_model_is_built_beside_the_serving_index_and_replaces_it_only_when_complete(monkeypatch, data):
    run(data)
    old = rs.CONCEPT_MODEL
    new = EmbeddingModel("mock", "concept-test-large", None, 96)
    monkeypatch.setattr(get_settings(), "rag_index_max_entries_per_run", 5)
    partial = run(data, model=new)
    assert partial["status"] == "BUILDING" and partial["covered"] == 5
    with rs.session_for(SessionLocal, data.user_id) as db:
        serving = rag_index.serving_version(db, data.user_id)
        assert (serving.model, serving.dimensions) == (old.model, old.dimensions)
    pack = _pack(data, "ml_churn")
    assert pack.index_version == serving.id and pack.mode == "hybrid"  # still the old, complete index
    monkeypatch.setattr(get_settings(), "rag_index_max_entries_per_run", 500)
    done = run(data, model=new)
    assert done["status"] == "SERVING"
    with rs.session_for(SessionLocal, data.user_id) as db:
        versions = {v.model: v.status for v in db.scalars(select(RagIndexVersion).where(
            RagIndexVersion.user_id == data.user_id))}
    assert versions == {old.model: "RETIRED", new.model: "SERVING"}
    rag_retrieval.clear_cache()
    pack = _pack(data, "ml_churn")
    assert pack.index_version == done["index_version"] and pack.mode == "hybrid"


def _pack(data, key):
    with rs.session_for(SessionLocal, data.user_id) as db:
        profile, job = rs.profile_and_job(db, data.jobs[key])
        return rag_retrieval.retrieve_evidence(db, profile, job)


def test_a_fact_added_while_a_version_builds_keeps_it_from_serving_until_covered(data):
    inner = rs.ConceptBackend()

    class AddsAFact:
        """Embeds normally; during the first call a person verifies a new fact (the build is mid-run)."""
        added = False

        def embed(self, model, texts, operation):
            if not self.added:
                self.added = True
                with rs.session_for(SessionLocal, data.user_id) as db:
                    db.add(Evidence(user_id=data.user_id, claim="Led a payments migration", verified=True,
                                    source="manual"))
                    db.commit()
            return inner.embed(model, texts, operation)

    first = run(data, AddsAFact())
    assert first["status"] == "BUILDING" and first["eligible"] == len(data.facts) + 1
    assert first["covered"] == len(data.facts)  # the new fact is pending, so the version does not serve yet
    second = run(data)
    assert second["status"] == "SERVING" and second["covered"] == second["eligible"] == len(data.facts) + 1


def test_vectors_of_different_sizes_are_never_compared(data):
    run(data)
    with rs.session_for(SessionLocal, data.user_id) as db:
        version = rag_index.serving_version(db, data.user_id)
        ids = {e.id: e.content_hash for e in entries(data, index_version_id=version.id)}
        assert vector_store.search(db, user_id=data.user_id, index_version_id=version.id, dimensions=96,
                                   query=[0.1] * 96, entry_hashes=ids, limit=10) == []
        assert vector_store.search(db, user_id=data.user_id + 10_000, index_version_id=version.id, dimensions=64,
                                   query=[0.1] * 64, entry_hashes=ids, limit=10) == []  # another account: nothing


def test_retired_indexes_are_purged_after_retention(monkeypatch, data):
    run(data)
    run(data, model=EmbeddingModel("mock", "concept-test-large", None, 96))
    monkeypatch.setattr(get_settings(), "rag_retired_retention_days", 0)
    with SessionLocal() as db:
        assert rag_index.purge_retired(db, rag_index.now() + rag_index.timedelta(seconds=1)) >= 1
    with rs.session_for(SessionLocal, data.user_id) as db:
        assert [v.status for v in db.scalars(select(RagIndexVersion).where(
            RagIndexVersion.user_id == data.user_id))] == ["SERVING"]
    assert vectors(data) == len(data.facts)


def test_an_account_without_an_embedding_model_is_skipped(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    user_id = make_user(SessionLocal)
    assert rag_index.run_account(SessionLocal, user_id)["skipped"] == "UNSUPPORTED_PROVIDER"


def test_the_scheduler_job_is_off_unless_vector_retrieval_is_on(monkeypatch, data):
    import asyncio
    from datetime import UTC, datetime

    from app.services import scheduled_jobs

    ctx = scheduled_jobs.JobContext(sessions=SessionLocal, trigger="manual", now=datetime.now(UTC).replace(tzinfo=None),
                                    worker_id="test", interval_minutes=10)
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", False)
    assert "skipped" in asyncio.run(scheduled_jobs.rag_index_job(ctx))
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    summary = asyncio.run(scheduled_jobs.rag_index_job(ctx))
    assert summary["embedded"] >= len(data.facts)  # the mock provider's own embedder, for every account
    assert get_settings().rag_index_interval_minutes == get_settings().rag_index_minutes


# ───────────── the tag API ─────────────

@pytest.fixture
def client(data, monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(data.user_id, "rag@jobhunt.test", False))
    return TestClient(app)


def test_the_evidence_bank_shows_and_changes_resume_tags(client, data):
    rows = {r["id"]: r for r in client.get("/api/evidence").json()}
    assert [t["id"] for t in rows[data.facts["churn"]]["resumes"]] == [data.resumes["data"]]
    assert rows[data.facts["mentoring"]]["all_resumes"] is True
    assert rows[data.facts["uplift_paper"]]["resumes"][0]["origin"] == "USER"
    out = client.put(f"/api/evidence/{data.facts['mentoring']}/resumes", json={"resume_ids": [data.resumes["backend"]]})
    assert out.status_code == 200 and [t["id"] for t in out.json()["resumes"]] == [data.resumes["backend"]]
    out = client.post("/api/evidence/bulk-resumes", json={"evidence_ids": [data.facts["pycon_talk"]],
                                                          "add": [data.resumes["data"]], "remove": []})
    assert out.status_code == 200
    choices = client.get("/api/evidence/resumes").json()
    assert {c["id"] for c in choices} == set(data.resumes.values()) and any(c["is_active"] for c in choices)
    created = client.post("/api/evidence", json={"claim": "Ran a design review guild", "resume_ids": [data.resumes["data"]]})
    rows = {r["id"]: r for r in client.get("/api/evidence").json()}
    assert [t["id"] for t in rows[created.json()["id"]]["resumes"]] == [data.resumes["data"]]


def test_the_tag_api_refuses_another_accounts_resume(client, data):
    other = rs.load(SessionLocal, make_user(SessionLocal))
    out = client.put(f"/api/evidence/{data.facts['mentoring']}/resumes", json={"resume_ids": [other.resumes["data"]]})
    assert out.status_code == 404
    out = client.put(f"/api/evidence/{other.facts['mentoring']}/resumes", json={"resume_ids": []})
    assert out.status_code == 404
    assert client.post("/api/evidence", json={"claim": "x", "resume_ids": [other.resumes["data"]]}).status_code == 404


def test_status_and_preview_are_reads(client, data):
    run(data)
    status = client.get("/api/rag/status").json()
    assert status["versions"][0]["status"] == "SERVING" and status["versions"][0]["vectors"] == len(data.facts)
    preview = client.get("/api/rag/preview", params={"job_id": data.jobs["data_kafka_conflict"]}).json()
    assert preview["resume_id"] == data.resumes["data"] and preview["fallback_reason"] == "MODEL_CALL_NOT_ALLOWED"
    assert data.facts["kafka_incidents"] not in preview["evidence_ids"]
    assert client.get("/api/rag/preview", params={"job_id": 999_999}).status_code == 404


def test_a_server_without_pgvector_runs_without_vectors():
    """No extension available: setup says why, creates nothing, and retrieval stays full-text (the app still starts;
    migration 0045 never touches the extension)."""

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    class NoVector:
        dialect = type("D", (), {"name": "postgresql"})()

        def __init__(self):
            self.sql: list[str] = []

        def execute(self, statement, *_args):
            self.sql.append(str(statement))
            return Result(None)

    conn = NoVector()
    available, schema, reason = vector_store._pg_setup(conn)
    assert (available, schema) == (False, None) and reason.startswith("VECTOR_UNAVAILABLE")
    assert not any("CREATE" in sql for sql in conn.sql)
