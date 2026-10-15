"""Hybrid retrieval: scope before scoring (account, the job's own resume, tags), pins beat similarity, citations are
real evidence ids, bounded packs, fallbacks, and the context builders unchanged unless RAG is switched on."""
from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import Evidence, EvidenceResumeLink, Job, KnowledgeAnswer, SystemLogEntry
from app.services import context_builder as cb
from app.services import evidence_tags, knowledge, rag_index, rag_retrieval
from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.rag_retrieval import verify_citations
from app.settings import get_settings
from tests import rag_support as rs
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", False)
    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "off")
    rag_retrieval.clear_cache()


@pytest.fixture
def data():
    return rs.load(SessionLocal, make_user(SessionLocal))


def retrieve(data, key, **kwargs):
    with rs.session_for(SessionLocal, data.user_id) as db:
        profile, job = rs.profile_and_job(db, data.jobs[key])
        return rag_retrieval.retrieve_evidence(db, profile, job, **kwargs)


def hybrid(monkeypatch, data):
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    rs.use_concepts(monkeypatch)
    summary = rag_index.run_account(SessionLocal, data.user_id, service=rs.concept_service(data.user_id))
    assert summary["status"] == "SERVING", summary


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_no_job_ever_receives_another_resumes_facts(monkeypatch, data, mode):
    if mode == "hybrid":
        hybrid(monkeypatch, data)
    for case in data.cases:
        pack = retrieve(data, case.key)
        assert not set(pack.evidence_ids) & case.forbidden, case.key
        assert pack.mode == mode


def test_a_pin_beats_similarity_with_conflicting_experience(monkeypatch, data):
    """Both resumes have Acme Kafka work; the job asks for incident reduction (the backend resume's wording) but is
    pinned to the data resume, so only the data resume's Kafka fact may answer, however similar the other is."""
    hybrid(monkeypatch, data)
    pack = retrieve(data, "data_kafka_conflict")
    assert pack.evidence_ids[0] == data.facts["kafka_feature_store"]
    assert data.facts["kafka_incidents"] not in pack.evidence_ids
    assert pack.resume_id == data.resumes["data"]
    with rs.session_for(SessionLocal, data.user_id) as db:  # the person re-pins the job: the scope follows
        db.get(Job, data.jobs["data_kafka_conflict"]).resume_id = data.resumes["backend"]
        db.commit()
    pack = retrieve(data, "data_kafka_conflict")
    assert pack.evidence_ids[0] == data.facts["kafka_incidents"]
    assert data.facts["kafka_feature_store"] not in pack.evidence_ids


def test_every_kind_of_pin_decides_the_scope(data):
    assert retrieve(data, "data_platform_setup").resume_id == data.resumes["data"]  # SETUP
    assert retrieve(data, "eng_manager").resume_id == data.resumes["backend"]  # ACTIVE
    assert retrieve(data, "python_perf").resume_id == data.resumes["backend"]  # AUTO, nothing matched: active
    assert retrieve(data, "ml_churn").resume_id == data.resumes["data"]  # USER


def test_untagged_evidence_applies_to_every_resume_and_tagged_evidence_to_its_resume(data):
    backend = set(retrieve(data, "eng_manager", limit=50, budget_tokens=10_000).evidence_ids)
    assert data.facts["mentoring"] in backend and data.facts["uplift_paper"] not in backend
    data_scope = rag_retrieval.scope(*_scope_args(data, "attrition_semantic"))[1]
    assert {data.facts["mentoring"], data.facts["uplift_paper"], data.facts["churn"]} <= data_scope


def _scope_args(data, key):
    db = rs.session_for(SessionLocal, data.user_id)
    profile, job = rs.profile_and_job(db, data.jobs[key])
    return db, profile, job


def test_a_persons_tags_override_the_automatic_ones(data):
    with rs.session_for(SessionLocal, data.user_id) as db:
        # A data fact the person also wants on backend jobs, and a backend fact they take off the backend resume.
        evidence_tags.set_resumes(db, data.user_id, data.facts["churn"], [data.resumes["data"], data.resumes["backend"]])
        evidence_tags.set_resumes(db, data.user_id, data.facts["aws_cost"], [])
        db.commit()
        backend = db.get(rs.Resume, data.resumes["backend"])
        allowed = evidence_tags.allowed_evidence_ids(db, data.user_id, backend)
        assert data.facts["churn"] in allowed and data.facts["aws_cost"] not in allowed
        # Re-verifying the backend resume does not bring the removed tag back.
        from app.services import resume_verification

        resume_verification.sync_evidence(db, backend)
        db.commit()
        assert data.facts["aws_cost"] not in evidence_tags.allowed_evidence_ids(db, data.user_id, backend)
        described = evidence_tags.describe(db, data.user_id, [db.get(Evidence, data.facts["churn"])])
        assert {t["id"] for t in described[data.facts["churn"]]["resumes"]} == set(data.resumes.values())


def test_tagging_another_accounts_resume_is_refused(data):
    other = rs.load(SessionLocal, make_user(SessionLocal))
    with rs.session_for(SessionLocal, data.user_id) as db:
        with pytest.raises(evidence_tags.TagError):
            evidence_tags.set_resumes(db, data.user_id, data.facts["mentoring"], [other.resumes["data"]])
        with pytest.raises(evidence_tags.TagError):
            evidence_tags.set_resumes(db, data.user_id, other.facts["mentoring"], [data.resumes["data"]])


def test_unverified_and_held_back_evidence_is_never_retrieved(data):
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.get(Evidence, data.facts["grpc_go"]).verified = False
        db.commit()
    pack = retrieve(data, "backend_kafka", exclude={data.facts["k8s_migration"]})
    assert data.facts["grpc_go"] not in pack.evidence_ids and data.facts["k8s_migration"] not in pack.evidence_ids


def test_accounts_never_see_each_other(monkeypatch, data):
    other = rs.load(SessionLocal, make_user(SessionLocal))
    hybrid(monkeypatch, data)
    rag_index.run_account(SessionLocal, other.user_id, service=rs.concept_service(other.user_id))
    mine = set(data.facts.values())
    for case in data.cases:
        assert set(retrieve(data, case.key).evidence_ids) <= mine
    with rs.session_for(SessionLocal, data.user_id) as db:  # someone else's job in my session: not even loadable
        assert db.get(Job, other.jobs["backend_kafka"]) is None


def test_the_pack_is_bounded_by_tokens_and_candidates(monkeypatch, data):
    pack = retrieve(data, "backend_kafka", budget_tokens=40)
    assert pack.used_tokens <= 40 and pack.truncated
    assert sum(item.tokens for item in pack.items) == pack.used_tokens
    monkeypatch.setattr(get_settings(), "rag_max_candidates", 2)
    assert len(retrieve(data, "backend_kafka").items) <= 2


def test_citations_are_checked_against_what_the_pack_offered(data):
    pack = retrieve(data, "backend_kafka")
    valid, invalid = verify_citations(pack, [pack.evidence_ids[0], data.facts["churn"], 999_999])
    assert valid == [pack.evidence_ids[0]] and invalid == sorted([data.facts["churn"], 999_999])
    assert all(item.projection()["id"] == item.evidence_id for item in pack.items)


def test_vector_fallbacks_say_why(monkeypatch, data):
    assert retrieve(data, "ml_churn").fallback_reason == "VECTOR_DISABLED"
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    assert retrieve(data, "ml_churn").fallback_reason == "NO_SERVING_INDEX"
    hybrid(monkeypatch, data)
    assert retrieve(data, "ml_churn", allow_model_call=False).fallback_reason == "MODEL_CALL_NOT_ALLOWED"

    def down(*_args, **_kwargs):
        raise EmbeddingError("UNAVAILABLE", "provider down")
    rag_retrieval.clear_cache()
    monkeypatch.setattr(EmbeddingService, "embed_query", down)
    pack = retrieve(data, "ml_churn")
    assert pack.fallback_reason == "EMBEDDING_UNAVAILABLE" and pack.mode == "lexical" and pack.items


def test_a_stale_vector_is_never_used(monkeypatch, data):
    hybrid(monkeypatch, data)
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.get(Evidence, data.facts["churn"]).claim = "Forecast customer attrition for a subscription product"
        db.commit()
        entry = db.scalar(select(rag_index.RagIndexEntry).where(
            rag_index.RagIndexEntry.source_id == data.facts["churn"], rag_index.RagIndexEntry.source_type == "evidence"))
        assert entry.status == "PENDING"  # marked by the flush listener, in the same transaction
    pack = retrieve(data, "attrition_semantic")
    churn = next(i for i in pack.items if i.evidence_id == data.facts["churn"])
    assert "vector" not in churn.scores and "attrition" in churn.claim  # found by its new text only


def test_suggested_answers_never_answer_and_hide_sensitive_text(monkeypatch):
    user_id = make_user(SessionLocal)
    with rs.session_for(SessionLocal, user_id) as db:
        db.add_all([KnowledgeAnswer(question="Do you require visa sponsorship to work in Germany?",
                                    question_key=knowledge.question_key("visa germany"), answer="No",
                                    verified=True, sensitive=True),
                    KnowledgeAnswer(question="What is your notice period at your current employer?",
                                    question_key=knowledge.question_key("notice"), answer="30 days", verified=True)])
        db.commit()
        question = "Will you need sponsorship for a work visa in the Netherlands?"
        before = knowledge.search(db, question)
        assert "suggestions" not in before  # RAG off: exactly the old answer
        monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "on")
        after = knowledge.search(db, question)
        assert after["status"] == before["status"] and after["answer"] == before["answer"]
        assert after["suggestions"] and all(s["answer"] is None and s["authoritative"] is False
                                            for s in after["suggestions"])
        notice = knowledge.search(db, "How long is your notice period?")
        assert all(s["authoritative"] is False for s in notice.get("suggestions", []))


# ───────────── the context builders: unchanged unless RAG is on ─────────────

def _ranking_context(data, key):
    with rs.session_for(SessionLocal, data.user_id) as db:
        profile, job = rs.profile_and_job(db, data.jobs[key])
        return cb.build_ranking_context(db, profile, job)


def test_off_is_the_deterministic_builder_exactly(monkeypatch, data):
    called = []
    monkeypatch.setattr(rag_retrieval, "retrieve_evidence", lambda *a, **k: called.append(1))
    off = _ranking_context(data, "backend_kafka")
    assert not called
    # The pre-RAG builder: retrieval's hook taken out entirely.
    monkeypatch.setattr(cb, "retrieved_evidence", lambda db, profile, job, task, terms, limit, deterministic, *_: deterministic)
    before = _ranking_context(data, "backend_kafka")
    assert off.to_dict() == before.to_dict()


def test_shadow_changes_nothing_and_logs_the_difference(monkeypatch, data):
    off = _ranking_context(data, "data_kafka_conflict")
    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "shadow")
    shadow = _ranking_context(data, "data_kafka_conflict")
    assert shadow.relevant_evidence == off.relevant_evidence
    assert shadow.to_dict() == off.to_dict()
    with SessionLocal() as db:
        row = db.scalar(select(SystemLogEntry).where(SystemLogEntry.operation == "rag_shadow",
                                                     SystemLogEntry.entity_id == data.jobs["data_kafka_conflict"]))
    assert row is not None and row.details["outside_resume_scope"]  # the old builder's cross-resume facts
    assert not set(row.details["outside_resume_scope"]) & set(row.details["evidence_ids"])
    assert "claim" not in str(row.details)  # ids and counts only


def test_on_takes_evidence_from_the_jobs_own_resume_only(monkeypatch, data):
    off = _ranking_context(data, "ml_churn")
    leaked_before = {e["id"] for e in off.relevant_evidence} & data.cases[1].forbidden
    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "on")
    on = _ranking_context(data, "ml_churn")
    ids = {e["id"] for e in on.relevant_evidence}
    assert not ids & data.cases[1].forbidden
    assert leaked_before  # the deterministic builder draws on every resume's facts; that is what RAG fixes
    assert on.relevant_experience == off.relevant_experience  # the resume's own bullets are untouched
    assert ids <= on.citable_ids | on.evidence_ids


def test_on_falls_back_to_the_deterministic_builder_when_retrieval_fails(monkeypatch, data):
    off = _ranking_context(data, "ml_churn")
    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "on")

    def broken(*_a, **_k):
        raise RuntimeError("index unavailable")
    monkeypatch.setattr(rag_retrieval, "retrieve_evidence", broken)
    assert _ranking_context(data, "ml_churn").relevant_evidence == off.relevant_evidence


def test_extracted_evidence_belongs_to_the_resume_it_was_extracted_from(data):
    with rs.session_for(SessionLocal, data.user_id) as db:
        active = db.get(rs.Resume, data.resumes["backend"])
        row = Evidence(user_id=data.user_id, claim="Wrote an internal Go style guide", verified=True,
                       source="llm_extraction")
        db.add(row)
        evidence_tags.link_extracted(db, active, [row])
        db.commit()
        links = db.scalars(select(EvidenceResumeLink).where(EvidenceResumeLink.evidence_id == row.id)).all()
        assert [(link.resume_id, link.origin) for link in links] == [(active.id, "AUTO")]
        data_resume = db.get(rs.Resume, data.resumes["data"])
        assert row.id not in evidence_tags.allowed_evidence_ids(db, data.user_id, data_resume)
        # Re-verifying the resume it came from keeps the tag (it is not one of that resume's verified facts).
        from app.services import resume_verification

        resume_verification.sync_evidence(db, active)
        db.commit()
        assert [link.resume_id for link in db.scalars(select(EvidenceResumeLink).where(
            EvidenceResumeLink.evidence_id == row.id)).all()] == [active.id]
        assert row.id not in evidence_tags.allowed_evidence_ids(db, data.user_id, data_resume)
        # Clearing its tags makes it apply to every resume, the one it came from included, as the Evidence Bank says.
        assert evidence_tags.set_resumes(db, data.user_id, row.id, [])["all_resumes"] is True
        db.commit()
        assert all(row.id in evidence_tags.allowed_evidence_ids(db, data.user_id, r) for r in (active, data_resume))
        db.execute(update(Evidence).where(Evidence.id == row.id).values(verified=False))
        db.commit()


def test_the_mcp_evidence_selection_follows_the_same_scope_when_on(monkeypatch, data):
    from app.services import ranker

    def selected():
        with rs.session_for(SessionLocal, data.user_id) as db:
            return {e["id"] for e in ranker.relevant_evidence(db, data.user_id, db.get(Job, data.jobs["ml_churn"]), 40)}
    forbidden = next(c.forbidden for c in data.cases if c.key == "ml_churn")
    assert selected() & forbidden  # off: account-wide, as before
    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "on")
    assert selected() and not selected() & forbidden


def test_on_tailoring_refuses_a_citation_of_another_resumes_fact(monkeypatch, data):
    from app.services.tailoring_results import (
        BulletIn,
        TailoredResumeIn,
        validate_tailored_resume,
        verified_evidence,
    )

    case = next(c for c in data.cases if c.key == "ml_churn")
    other = min(case.forbidden)  # verified, but only on the backend resume; the job uses the data resume

    def unknown_evidence():
        with rs.session_for(SessionLocal, data.user_id) as db:
            profile, job = rs.profile_and_job(db, data.jobs["ml_churn"])
            issues = validate_tailored_resume(db, profile, job, TailoredResumeIn(
                job_id=job.id, resume_bullets=[BulletIn(text="Built event pipelines", evidence_ids=[other])]))
            return [i for i in issues if i["code"] == "UNKNOWN_EVIDENCE"], other in verified_evidence(db, job)
    assert unknown_evidence() == ([], True)  # off: the account-wide check, as before
    monkeypatch.setattr(get_settings(), "rag_retrieval_mode", "on")
    issues, known = unknown_evidence()
    assert not known and len(issues) == 1 and "this job's resume" in issues[0]["message"]
