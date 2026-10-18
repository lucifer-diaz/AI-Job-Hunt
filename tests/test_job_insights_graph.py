"""The Job Insights graph (Phase 4) against the current research path, on PostgreSQL (TEST_DATABASE_URL), and the
provenance check it adds (app.services.job_insights.provenance), which needs no database.

Parity builds the same approved job in two accounts with the same fake sources and the mock model; one is researched by
`service.process` (JOB_INSIGHTS_GRAPH_MODE=off), the other by the graph (on). The API's view of each must be the same,
apart from ids and timestamps.
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agents import checkpoint
from app.models import (
    AgentEffect,
    AgentThread,
    InsightObservation,
    Job,
    JobInsight,
    JobInsightTask,
    LLMRequestLog,
    ResumeVariant,
)
from app.services import scheduled_jobs as sj
from app.services import workflow
from app.services.job_insights import provenance, service, views
from app.services.job_insights.sources import PostingSource, RecruiterSource
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from tests import qualification_support as qs
from tests.test_job_insights import FakeSource, obs, salary
from tests.test_ranker import add_owner

VOLATILE = {"generated_at", "refreshed_at", "last_attempt_at", "last_successful_at", "fetched_at", "retrieved_at",
            "created_at", "completed_at", "started_at", "updated_at", "next_attempt_at", "application_id", "task"}


def sources() -> list[Any]:
    return [PostingSource(), RecruiterSource(),
            FakeSource("glassdoor_compensation", [salary("GLASSDOOR", 150000, 190000, sample_size=12),
                                                  salary("PAYSCALE", 100000, 120000, sample_size=40)]),
            FakeSource("official_company", [obs("VALUES", "OFFICIAL_COMPANY", {"value": "Customer obsession"}),
                                            obs("COMPANY", "OFFICIAL_COMPANY", {"field": "mission",
                                                                                "text": "Make logistics simple"})],
                       source_type="OFFICIAL_COMPANY", company_level=True),
            FakeSource("glassdoor_reviews", [obs("CULTURE", "GLASSDOOR", {"headline": "Fast paced", "pros": "Smart",
                                                                          "cons": "Long hours"})], company_level=True),
            FakeSource("glassdoor_interviews", [obs("INTERVIEW_QUESTION", "GLASSDOOR",
                                                    {"question": "Design a rate limiter", "stage": "ONSITE"})])]


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch):
    settings = get_settings()
    for name, value in (("agent_graphs_enabled", True), ("job_insights_graph_mode", "off"), ("llm_provider", "mock"),
                        ("job_insights_enabled", True)):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(service, "SOURCES", sources())
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    return qs.app_sessions(pg_engine)


def approved_job(sessions, user_id: int, *, approve: bool = True) -> int:
    with sessions() as db:
        db.info["user_id"] = user_id
        job = Job(user_id=user_id, source="test", source_ref=uuid.uuid4().hex, title="Senior Backend Engineer",
                  company="Northwind Labs", location="Singapore", description="Build reliable Python services. " * 30,
                  url=f"https://jobs.example.com/{uuid.uuid4().hex}", status="tailored", state=S.QUALIFIED,
                  salary_min=160000, salary_max=200000, salary_currency="SGD", salary_interval="year",
                  salary_source="employer")
        db.add(job)
        db.flush()
        db.add(ResumeVariant(user_id=user_id, job_id=job.id, version=1, reviewer_score=90, markdown="# x"))
        db.flush()
        if approve:
            workflow.approve_job(db, job)
        db.commit()
        return job.id


def research(sessions) -> dict[str, Any]:
    return asyncio.run(sj.job_insights(qs.ctx(sessions)))


def view(sessions, user_id: int, job_id: int) -> dict[str, Any]:
    with sessions() as db:
        db.info["user_id"] = user_id
        out = views.insight_view(db, db.get(Job, job_id))
        ids = sorted(db.scalars(select(InsightObservation.id).join(JobInsight).where(JobInsight.job_id == job_id)))
    return _normalise(out, {i: n for n, i in enumerate(ids)}, job_id)


def _normalise(value: Any, ids: dict[int, int], job_id: int) -> Any:
    """Observation ids → their rank within the job; the job's own id and every timestamp left out."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in VOLATILE:
                continue
            if key in ("source_ids", "value_source_ids") and isinstance(item, list):
                out[key] = [ids.get(i, i) for i in item]
            elif key == "id" and isinstance(item, int):
                out[key] = ids.get(item, "job" if item == job_id else item)
            else:
                out[key] = _normalise(item, ids, job_id)
        return out
    if isinstance(value, list):
        return [_normalise(v, ids, job_id) for v in value]
    return value


# ───────────── parity with the current path ─────────────

def test_the_graph_saves_what_the_current_path_saves(sessions, monkeypatch):
    legacy_user, graph_user = add_owner(sessions), add_owner(sessions)
    legacy_job = approved_job(sessions, legacy_user)
    research(sessions)
    monkeypatch.setattr(service, "SOURCES", sources())
    monkeypatch.setattr(get_settings(), "job_insights_graph_mode", "on")
    graph_job = approved_job(sessions, graph_user)
    research(sessions)

    legacy, new = view(sessions, legacy_user, legacy_job), view(sessions, graph_user, graph_job)
    assert legacy["status"] == new["status"] == "COMPLETED"
    for key in ("compensation", "compensation_prep", "company", "interview", "questions_to_ask", "risks",
                "unknowns", "sources", "identity", "overview", "prompt_versions", "model"):
        assert new[key] == legacy[key], key
    with sessions() as db:
        task = db.scalar(select(JobInsightTask).where(JobInsightTask.job_id == graph_job))
        thread = db.get(AgentThread, task.agent_thread_id)
        assert thread.status == "COMPLETED" and thread.graph == "job_insights"
        assert thread.result["provenance_violations"] == 0 and task.staged is None
        keys = sorted(db.scalars(select(AgentEffect.effect_key).where(AgentEffect.agent_thread_id == thread.id)))
        assert [k.split(":")[0] for k in keys] == ["insights.collect", "insights.persist", "insights.synthesize"]
        # Employer pay and market estimates stay apart; the conflicting estimates are listed, never averaged.
        comp = db.scalar(select(JobInsight.compensation).where(JobInsight.job_id == graph_job))
        assert comp["employer_posted_range"]["low"] == 160000
        assert comp["market_range"] is None or comp["market_range"]["label"].startswith("Market estimate")


def test_each_source_and_each_model_call_happens_once_as_on_the_current_path(sessions, monkeypatch):
    calls = {}
    for mode in ("off", "on"):
        monkeypatch.setattr(get_settings(), "job_insights_graph_mode", mode)
        fakes = sources()
        monkeypatch.setattr(service, "SOURCES", fakes)
        user = add_owner(sessions)
        approved_job(sessions, user)
        research(sessions)
        with sessions() as db:
            model_calls = db.scalar(select(func.count()).select_from(LLMRequestLog)
                                    .where(LLMRequestLog.user_id == user))
        calls[mode] = ([f.calls for f in fakes if hasattr(f, "calls")], model_calls)
    assert calls["on"] == calls["off"]
    assert all(n == 1 for n in calls["on"][0])


def test_approval_never_waits_for_research_and_stands_when_it_fails(sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_insights_graph_mode", "on")
    monkeypatch.setattr(service, "SOURCES", [FakeSource("glassdoor_compensation", raises=RuntimeError("down"))])
    user = add_owner(sessions)
    job_id = approved_job(sessions, user)
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "approved" and job.state is S.READY_TO_APPLY  # before any research ran
    research(sessions)
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "approved" and job.application.status == "approved"


def test_there_is_no_research_before_approval(sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_insights_graph_mode", "on")
    user = add_owner(sessions)
    job_id = approved_job(sessions, user, approve=False)
    summary = research(sessions)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(JobInsightTask)
                         .where(JobInsightTask.job_id == job_id)) == 0
        assert db.scalar(select(func.count()).select_from(AgentThread)) == 0
    assert summary.get("claimed", 0) == 0


def test_shadow_rechecks_a_finished_run_without_writing(sessions, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_insights_graph_mode", "shadow")
    user = add_owner(sessions)
    job_id = approved_job(sessions, user)
    research(sessions)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentThread)) == 0  # in memory: nothing stored
        task = db.scalar(select(JobInsightTask).where(JobInsightTask.job_id == job_id))
        assert task.status == "COMPLETED" and task.agent_thread_id is None
    from app.services.job_insights import graph as runner

    result = runner.shadow(sessions, user, job_id, task.id)
    assert result["code"] == "PARITY" and result["provenance_violations"] == 0


# ───────────── the provenance check (no database) ─────────────

OBS = [
    {"id": 1, "category": "VALUES", "source_type": "OFFICIAL_COMPANY", "match_status": "ACCEPTED"},
    {"id": 2, "category": "CULTURE", "source_type": "GLASSDOOR", "match_status": "ACCEPTED"},
    {"id": 3, "category": "INTERVIEW_QUESTION", "source_type": "GLASSDOOR", "match_status": "ACCEPTED"},
    {"id": 4, "category": "COMPENSATION", "source_type": "EMPLOYER", "match_status": "ACCEPTED"},
    {"id": 5, "category": "COMPENSATION", "source_type": "GLASSDOOR", "match_status": "ACCEPTED"},
]
SECTIONS = {
    "compensation": {"employer_posted_range": {"source_ids": [4]}, "market_range": {"source_ids": [5]}},
    "company": {"official_values": [{"value": "Ownership", "kind": "FACT", "source_ids": [1]}],
                "culture_themes": [{"theme": "Fast", "kind": "DERIVED", "source_ids": [2]}],
                "profile_facts": [], "mission": [], "summary": None},
    "reported_questions": [{"question": "Design a cache", "origin": "REPORTED", "kind": "FACT", "source_ids": [3]}],
    "generated_questions": [{"question": "Tell me about scale", "origin": "ROLE_DERIVED", "kind": "GENERATED"}],
    "questions_to_ask": [{"question": "Team size?", "kind": "GENERATED"}],
}


def test_clean_sections_pass_the_check():
    assert provenance.check(SECTIONS, OBS).count == 0


@pytest.mark.parametrize("mutate,rule", [
    (lambda s: s["company"]["official_values"][0].update(source_ids=[2]), "WRONG_CATEGORY"),
    (lambda s: s["company"]["culture_themes"][0].update(source_ids=[1]), "WRONG_CATEGORY"),
    (lambda s: s["reported_questions"][0].update(source_ids=[]), "MISSING_SOURCE"),
    (lambda s: s["reported_questions"][0].update(origin="ROLE_DERIVED"), "NOT_REPORTED"),
    (lambda s: s["generated_questions"][0].update(source_ids=[3]), "GENERATED_WITH_SOURCE"),
    (lambda s: s["generated_questions"][0].update(kind="FACT"), "GENERATED_MISLABELLED"),
    (lambda s: s["company"]["official_values"][0].update(source_ids=[99]), "UNKNOWN_SOURCE"),
    (lambda s: s["compensation"]["employer_posted_range"].update(source_ids=[5]), "WRONG_SOURCE_TYPE"),
    (lambda s: s["compensation"]["market_range"].update(source_ids=[4]), "WRONG_SOURCE_TYPE"),
])
def test_each_provenance_rule(mutate, rule):
    sections = copy.deepcopy(SECTIONS)
    mutate(sections)
    report = provenance.check(sections, OBS)
    assert report.count == 1 and report.violations[0]["rule"] == rule


def test_enforce_drops_a_bad_item_and_never_relabels_it():
    sections = copy.deepcopy(SECTIONS)
    sections["reported_questions"].append({"question": "Invented", "origin": "REPORTED", "kind": "FACT",
                                           "source_ids": []})
    sections["generated_questions"].append({"question": "Attributed", "origin": "ROLE_DERIVED", "kind": "GENERATED",
                                            "source_ids": [3]})
    kept, report = provenance.enforce(sections, OBS)
    assert report.count == 2
    assert [q["question"] for q in kept["reported_questions"]] == ["Design a cache"]
    assert [q["question"] for q in kept["generated_questions"]] == ["Tell me about scale"]
    assert sections["reported_questions"][-1]["question"] == "Invented"  # the input is not changed
