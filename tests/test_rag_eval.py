"""Retrieval evaluation on the labelled benchmark (tests/fixtures/rag_eval/dataset.json): recall and precision at k,
citation correctness, zero scope leaks, bounded tokens and latency — and the deterministic builder compared with
retrieval on the same jobs. The thresholds are the merge gate for this fixture, not claims about a real model."""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.services import rag_eval, rag_index, rag_retrieval
from app.settings import get_settings
from tests import rag_support as rs
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", False)
    rag_retrieval.clear_cache()


@pytest.fixture
def data():
    return rs.load(SessionLocal, make_user(SessionLocal))


def report(data, allow_model_call=True):
    with rs.session_for(SessionLocal, data.user_id) as db:
        def retrieve(job):
            profile, _ = rs.profile_and_job(db, job.id)
            return rag_retrieval.retrieve_evidence(db, profile, job, allow_model_call=allow_model_call)
        return rag_eval.evaluate(db, data.user_id, data.cases, retrieve)


def test_metrics():
    assert rag_eval.recall_at_k([1, 2, 3], {2, 9}, 2) == 0.5
    assert rag_eval.recall_at_k([1], set(), 5) is None
    assert rag_eval.precision_at_k([1, 2, 3, 4], {2, 4}, 4) == 0.5
    assert rag_eval.precision_at_k([], {1}, 3) is None
    assert rag_eval.percentile([5, 1, 9, 3], 0.95) == 9 and rag_eval.percentile([], 0.5) is None


def test_keyword_retrieval_meets_the_benchmark(data):
    out = report(data)
    assert out.leaks == 0 and out.citation_correctness == 1.0
    assert out.recall_at_5 >= 0.95 and out.recall_at_10 == 1.0
    assert out.r_precision >= 0.8 and out.precision_at_3 >= 0.5
    assert out.token_reduction > 0.3  # the pack is a fraction of the whole evidence bank
    assert out.latency_p95_ms < 1000
    assert out.fallback_reasons == {"VECTOR_DISABLED": len(data.cases)}


def test_hybrid_retrieval_meets_the_benchmark_and_finds_meaning_keywords_miss(monkeypatch, data):
    lexical = report(data)
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    rs.use_concepts(monkeypatch)
    rag_index.run_account(SessionLocal, data.user_id, service=rs.concept_service(data.user_id))
    hybrid = report(data)
    assert hybrid.leaks == 0 and hybrid.citation_correctness == 1.0 and not hybrid.fallback_reasons
    assert hybrid.recall_at_5 >= lexical.recall_at_5 and hybrid.r_precision >= lexical.r_precision
    assert hybrid.latency_p95_ms < 1000


def test_the_deterministic_builder_leaks_other_resumes_facts_and_retrieval_does_not(data):
    """The motivating defect: the old selection is account-wide. Retrieval removes exactly the out-of-scope facts."""
    with rs.session_for(SessionLocal, data.user_id) as db:
        profile, _ = rs.profile_and_job(db, data.jobs["ml_churn"])
        jobs = [db.get(rs.Job, data.jobs[c.key]) for c in data.cases]
        rows = rag_eval.compare_builders(db, profile, jobs)
    forbidden = {c.job_id: c.forbidden for c in data.cases}
    old_leaks = sum(len(set(r["deterministic"]) & forbidden[r["job_id"]]) for r in rows)
    new_leaks = sum(len(set(r["retrieval"]) & forbidden[r["job_id"]]) for r in rows)
    assert old_leaks > 0 and new_leaks == 0
    for r in rows:  # everything retrieval drops from the old selection is out of the job's scope or irrelevant
        assert set(r["deterministic_out_of_scope"]) <= set(r["only_deterministic"])
