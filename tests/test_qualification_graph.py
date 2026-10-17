"""The qualification graph (Phase 3) against the current path, on PostgreSQL (TEST_DATABASE_URL; skipped otherwise).

Every parity test builds the same job twice: once in an account qualified by the current path
(QUALIFICATION_GRAPH_MODE=off), once in an account qualified by the graph (on). Both run through the real scheduler
passes with the same fake ranking answers, and the test compares the outcome, the transition history, the task rows
and the paid work done for each.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, update

from app.agents import checkpoint, effects
from app.agents import qualification as graph
from app.agents import runtime
from app.infrastructure.browser import set_browser_service
from app.models import (
    AgentEffect,
    AgentThread,
    AppSetting,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobQualificationTask,
    JobRankingTask,
    LLMRequestLog,
    Resume,
)
from app.services import orchestration, qualification, ranker, resume_matching, workers
from app.services import scheduled_jobs as sj
from app.services.lifecycle import STOPPED_BY_USER
from app.services.llm import LLMResponseError
from tests import qualification_support as qs
from tests.jd_support import FakeBrowser, PostingServer, page
from tests.test_ranker import RESUME, FakeModel, add_owner


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch):
    qs.configure(monkeypatch, "on")
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    from app.services import backoff, server_settings

    backoff.reset_cache()
    server_settings.reset_cache() if hasattr(server_settings, "reset_cache") else None
    yield qs.app_sessions(pg_engine)
    set_browser_service(None)


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(ranker, "LLMClient", fake)
    return fake


@pytest.fixture
def parses(monkeypatch):
    return qs.count_parses(monkeypatch)


@pytest.fixture
def server():
    with PostingServer() as posting_server:
        yield posting_server


def both(sessions, monkeypatch, build, *, browser=None):
    """Build the case in a current-path account and a graph account; return both jobs' facts."""
    legacy_user, graph_user = add_owner(sessions), add_owner(sessions)
    qs.set_mode(monkeypatch, "off")
    legacy = build(legacy_user)
    qs.drive(sessions, browser=browser)
    qs.set_mode(monkeypatch, "on")
    new = build(graph_user)
    qs.drive(sessions, browser=browser)
    return legacy, new


# ───────────── golden parity: qualified, archived and gaps ─────────────

GOLDEN = [
    ("qualified", {}, "QUALIFIED"),
    ("archived_low_score", {"overall_fit_score": 40}, "ARCHIVED"),
    ("archived_fatal_gap", {"fatal_gaps": ["Requires an active security clearance"]}, "ARCHIVED"),
    ("qualified_with_missing_requirement", {"missing_requirements": ["Go", "Rust"], "fatal_gaps": []}, "QUALIFIED"),
    ("archived_low_confidence", {"confidence": 0.2}, "ARCHIVED"),
]


@pytest.mark.parametrize("case,answer,expected", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_decisions_and_history_match_the_current_path(sessions, monkeypatch, model, parses, case, answer, expected):
    built = []

    def build(user_id):
        job_id = qs.new_job(sessions, user_id)
        model.answer(job_id, answer)
        built.append(job_id)
        return job_id

    legacy_id, graph_id = both(sessions, monkeypatch, build)
    legacy, new = qs.facts(sessions, legacy_id), qs.facts(sessions, graph_id)
    assert legacy["state"] == expected and new["state"] == expected
    for key in ("state", "status", "domain_match", "history", "tasks", "rankings", "decision"):
        assert new[key] == legacy[key], key
    assert model.calls.count(legacy_id) == model.calls.count(graph_id) == 1
    assert parses.parses.count(legacy_id) == parses.parses.count(graph_id) == 1
    task = qs.qualification_tasks(sessions, graph_id)[-1]
    assert (task.status, task.outcome, task.result_code) == ("COMPLETED", expected, "VALIDATED")
    assert task.agent_thread_id and qs.thread_of(sessions, task).status == "COMPLETED"


def test_a_hard_filtered_job_matches_the_current_path(sessions, monkeypatch, model, parses):
    def build(user_id):
        with sessions() as db:
            prefs = {"ranking": {"exclude_companies": ["Initech"]}}
            row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
            if row is None:
                db.add(AppSetting(user_id=user_id, preferences=prefs))
            else:
                row.preferences = {**(row.preferences or {}), **prefs}
            db.commit()
        return qs.new_job(sessions, user_id, company="Initech")

    legacy_id, graph_id = both(sessions, monkeypatch, build)
    legacy, new = qs.facts(sessions, legacy_id), qs.facts(sessions, graph_id)
    assert new["state"] == legacy["state"]
    assert new["history"] == legacy["history"]
    assert model.calls.count(graph_id) == model.calls.count(legacy_id)


# ───────────── admission first: hidden jobs cost nothing ─────────────

def _no_work(sessions, job_id):
    with sessions() as db:
        for model_ in (JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask):
            assert db.scalar(select(func.count()).select_from(model_).where(model_.job_id == job_id)) == 0
        assert db.scalar(select(func.count()).select_from(LLMRequestLog)
                         .where(LLMRequestLog.workflow_entity_id == job_id)) == 0


def test_a_new_job_outside_the_domain_never_reaches_recovery_parsing_or_a_model(sessions, model, parses, server):
    user_id = add_owner(sessions)
    browser = FakeBrowser()
    job_id = qs.new_job(sessions, user_id, title="Account Executive", description=qs.STUB, url=server.url("posting.html"))
    qs.drive(sessions, browser=browser)
    fact = qs.facts(sessions, job_id)
    assert fact["domain_match"] == "NOT_MATCHING" and fact["state"] == "SKIPPED"
    _no_work(sessions, job_id)
    assert model.calls == [] and parses.parses == [] and server.hits == [] and browser.calls == []
    assert qs.qualification_tasks(sessions, job_id) == []  # hidden at import: not even a graph run


def test_an_old_job_is_admitted_by_the_graph_before_anything_is_spent(sessions, model, parses, server):
    """A job from before admission existed (no verdict, unusable text, a URL) goes through the graph's admission
    first; hidden there, it gets no recovery, parse, browser or model call."""
    user_id = add_owner(sessions)
    browser = FakeBrowser()
    job_id = qs.old_job(sessions, user_id, title="Senior Account Executive", description=qs.STUB,
                        url=server.url("posting.html"))
    qs.drive(sessions, browser=browser)
    fact = qs.facts(sessions, job_id)
    assert fact["domain_match"] == "NOT_MATCHING" and fact["state"] == "SKIPPED"
    _no_work(sessions, job_id)
    assert model.calls == [] and parses.parses == [] and server.hits == [] and browser.calls == []
    task = qs.qualification_tasks(sessions, job_id)[-1]
    assert (task.outcome, task.step) == ("HIDDEN", graph.ADMISSION)


def test_a_job_outside_its_searchs_scope_is_hidden_by_the_graph_too(sessions, model, parses):
    user_id = add_owner(sessions)
    run_id = qs.search_run(sessions, user_id, ["Data Scientist"])
    job_id = qs.old_job(sessions, user_id, title="Pastry Chef", search_run_id=run_id)
    qs.drive(sessions)
    assert qs.facts(sessions, job_id)["domain_match"] == "NOT_MATCHING"
    _no_work(sessions, job_id)
    assert model.calls == []


# ───────────── descriptions: recovered, blocked, removed ─────────────

def _recovery_case(sessions, monkeypatch, server, path, *, browser=None):
    titles = {}

    def build(user_id):
        title = f"Platform Engineering Manager {user_id}"
        job_id = qs.new_job(sessions, user_id, title=title, description=qs.STUB, url=server.url(path))
        titles[job_id] = title
        return job_id

    legacy_id, graph_id = both(sessions, monkeypatch, build, browser=browser or FakeBrowser())
    return (qs.facts(sessions, legacy_id, title=titles[legacy_id]),
            qs.facts(sessions, graph_id, title=titles[graph_id]), legacy_id, graph_id)


def test_a_recovered_description_is_parsed_ranked_and_decided_like_the_current_path(sessions, monkeypatch, model,
                                                                                    parses, server):
    legacy, new, legacy_id, graph_id = _recovery_case(sessions, monkeypatch, server, "posting.html")
    assert legacy["state"] == new["state"] == "QUALIFIED"
    assert new["history"] == legacy["history"]  # the description wait of the current path left out
    assert new["tasks"]["recovery"] == legacy["tasks"]["recovery"] == [("COMPLETED", "RECOVERED")]
    assert model.calls.count(graph_id) == model.calls.count(legacy_id) == 1
    assert parses.parses.count(graph_id) <= parses.parses.count(legacy_id)
    assert server.hits.count("/posting.html") == 2  # one fetch per job
    task = qs.qualification_tasks(sessions, graph_id)[-1]
    assert task.outcome == "QUALIFIED"


def test_a_blocked_description_is_never_bypassed_and_ends_where_the_current_path_leaves_it(sessions, monkeypatch,
                                                                                         model, parses, server):
    """A login wall is recorded BLOCKED and never retried in the browser; the stub the board sent is not enough to
    rank, so the job is skipped for want of a description, as on the current path. The graph does not parse the
    stub either (the current path's backlog sweep does, for nothing)."""
    browser = FakeBrowser({"login.html": (200, page("posting"))})  # it could get through: it must not be tried
    legacy, new, legacy_id, graph_id = _recovery_case(sessions, monkeypatch, server, "login.html", browser=browser)
    assert legacy["state"] == new["state"] == "SKIPPED"
    assert new["history"] == legacy["history"]
    assert new["tasks"]["recovery"] == legacy["tasks"]["recovery"] == [("BLOCKED", "LOGIN_REQUIRED")]
    assert browser.calls == [] and model.calls == []
    assert parses.parses.count(graph_id) == 0 and parses.parses.count(graph_id) <= parses.parses.count(legacy_id)
    task = qs.qualification_tasks(sessions, graph_id)[-1]
    assert (task.outcome, task.result_code) == ("BLOCKED", "NO_DESCRIPTION")


def test_a_removed_posting_is_deleted_with_a_tombstone_and_its_run_stops(sessions, monkeypatch, model, parses,
                                                                        server):
    legacy, new, _, graph_id = _recovery_case(sessions, monkeypatch, server, "gone")
    assert legacy["deleted"] and new["deleted"] and legacy["tombstone"] == new["tombstone"] == "JOB_REMOVED"
    assert qs.qualification_tasks(sessions, graph_id) == []  # deleted with the job
    with sessions() as db:
        threads = db.scalars(select(AgentThread).where(AgentThread.graph == graph.GRAPH)).all()
        assert threads and all(t.status in ("STOPPED", "COMPLETED") for t in threads)
    assert model.calls == [] and parses.parses == []


# ───────────── resume decisions ─────────────

def _resume(sessions, user_id, name, *, roles=None):
    from app.models import CandidateProfile

    with sessions() as db:
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
        version = (db.scalar(select(func.max(Resume.version)).where(Resume.user_id == user_id)) or 0) + 1
        resume = Resume(user_id=user_id, candidate_profile_id=profile.id, name=name, version=version,
                        raw_text=f"{name}. {RESUME}", raw_text_hash=f"h{version}",
                        roles=roles or [], is_active=False)
        db.add(resume)
        db.commit()
        return resume.id


def test_a_pinned_resume_is_never_changed_and_is_the_one_the_run_scopes_evidence_to(sessions, model, parses):
    user_id = add_owner(sessions)
    pinned = _resume(sessions, user_id, "Pinned")
    _resume(sessions, user_id, "Tagged", roles=["Engineering Manager"])  # would win an automatic match
    job_id = qs.old_job(sessions, user_id, resume_id=pinned, resume_choice=resume_matching.USER)
    qs.drive(sessions)
    fact = qs.facts(sessions, job_id)
    assert (fact["resume_id"], fact["resume_choice"]) == (pinned, resume_matching.USER)
    thread = qs.thread_of(sessions, qs.qualification_tasks(sessions, job_id)[-1])
    assert thread.result["resume_id"] == pinned and thread.result["resume_choice"] == resume_matching.USER


# ───────────── leases: concurrent claims, restart ─────────────

def test_two_workers_claiming_at_once_never_get_the_same_task(sessions, model):
    user_id = add_owner(sessions)
    for _ in range(6):
        qs.new_job(sessions, user_id)
    barrier, got = threading.Barrier(2), []

    def claim(name):
        barrier.wait()
        with sessions() as db:
            got.append([c.task_id for c in qualification.claim(db, worker_id=name, limit=10)])

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(claim, ("a", "b")))
    claimed = got[0] + got[1]
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 6


@pytest.mark.parametrize("point,how", [("rank:after_enqueue", "exit"), ("parse:after_enqueue", "hang")])
def test_a_worker_killed_mid_run_is_continued_with_no_repeated_work(sessions, pg_url, model, parses, tmp_path,
                                                                    point, how):
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    from tests.conftest import WORKFLOW_TEST_SCHEMA

    user_id = add_owner(sessions)
    job_id = qs.new_job(sessions, user_id)
    if point.startswith("rank"):  # get the run to its rank step: it waits for the parse first
        qs.drive(sessions, passes=("qualification", "jd_parsing"), rounds=1, settle=False)
    marker = tmp_path / "marker"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root), "AGENT_GRAPHS_ENABLED": "true", "QUALIFICATION_GRAPH_MODE": "on",
           "RAG_VECTOR_ENABLED": "false", "RAG_RETRIEVAL_MODE": "off", "LLM_PROVIDER": "mock"}
    proc = subprocess.Popen([sys.executable, "-m", "tests.qualification_proc", pg_url, WORKFLOW_TEST_SCHEMA,
                             str(user_id), point, how, str(marker)], cwd=root, env=env)
    deadline = time.monotonic() + 60
    while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
        assert proc.poll() is None or marker.exists(), f"worker exited with {proc.returncode}"
        time.sleep(0.1)
    assert marker.read_text() == point
    if how == "hang":
        proc.send_signal(signal.SIGKILL)
    proc.wait(30)
    with sessions() as db:  # the dead worker's leases run out
        past = qs.utcnow()
        db.execute(update(JobQualificationTask).values(lock_expires_at=past))
        db.execute(update(AgentThread).where(AgentThread.status == "RUNNING").values(lock_expires_at=past))
        db.commit()
    qs.drive(sessions)
    fact = qs.facts(sessions, job_id)
    assert fact["state"] == "QUALIFIED"
    assert len(fact["tasks"]["ranking"]) == 1 and len(fact["tasks"]["parse"]) == 1
    assert model.calls.count(job_id) == 1 and parses.parses.count(job_id) == 1
    with sessions() as db:
        keys = db.scalars(select(AgentEffect.effect_key)).all()
    assert len(keys) == len(set(keys))
    task = qs.qualification_tasks(sessions, job_id)[-1]
    assert (task.status, task.outcome) == ("COMPLETED", "QUALIFIED")


# ───────────── model failures ─────────────

@pytest.mark.parametrize("reply", [LLMResponseError("provider returned an empty answer"),
                                   ("raw", {"job_id": 0, "not": "a ranking"})], ids=["failed_model", "invalid_json"])
def test_a_model_that_keeps_failing_fails_the_job_like_the_current_path(sessions, monkeypatch, model, parses, reply):
    def build(user_id):
        job_id = qs.new_job(sessions, user_id)
        model.answer(job_id, reply)
        return job_id

    legacy_id, graph_id = both(sessions, monkeypatch, build)
    legacy, new = qs.facts(sessions, legacy_id), qs.facts(sessions, graph_id)
    assert legacy["state"] == new["state"] == "FAILED"
    assert new["history"] == legacy["history"]
    assert model.calls.count(graph_id) == model.calls.count(legacy_id)
    task = qs.qualification_tasks(sessions, graph_id)[-1]
    assert (task.outcome, task.result_code) == ("FAILED", "RANKING_FAILED")


# ───────────── a person's controls: kill, retry ─────────────

def test_a_stopped_run_stays_stopped_until_retried(sessions, model, parses):
    user_id = add_owner(sessions)
    job_id = qs.new_job(sessions, user_id)
    qs.drive(sessions, passes=("qualification",), rounds=1, settle=False)  # the run waits for its parse
    task = qs.qualification_tasks(sessions, job_id)[-1]
    assert (task.status, task.waiting_for) == ("PENDING", "JD_PARSE")
    with sessions() as db:
        db.info["user_id"] = user_id
        workers.kill(db, user_id, "qualification", [task.id])
    stopped = qs.qualification_tasks(sessions, job_id)[-1]
    assert (stopped.status, stopped.last_error, stopped.outcome) == ("SKIPPED", STOPPED_BY_USER, "STOPPED")
    assert qs.thread_of(sessions, stopped).status == "STOPPED"
    qs.drive(sessions)  # the parse finishes; no sweep takes the job on behind the person's back
    assert qs.facts(sessions, job_id)["state"] == "DISCOVERED" and model.calls == []
    assert len(qs.qualification_tasks(sessions, job_id)) == 1
    with sessions() as db:
        db.info["user_id"] = user_id
        out = workers.retry(db, user_id, "qualification", [stopped.id])
    assert out["retried"]
    qs.drive(sessions)
    assert qs.facts(sessions, job_id)["state"] == "QUALIFIED"
    tasks = qs.qualification_tasks(sessions, job_id)
    assert len(tasks) == 2 and tasks[-1].outcome == "QUALIFIED" and tasks[-1].agent_thread_id != stopped.agent_thread_id


# ───────────── duplicates: delivery, dispatch, replay ─────────────

def test_a_duplicate_delivery_and_a_repeated_dispatch_make_one_run_and_one_call(sessions, model, parses):
    user_id = add_owner(sessions)
    first = qs.new_job(sessions, user_id, ref="board-123")
    again = qs.new_job(sessions, user_id, ref="board-123")  # the same posting delivered twice
    assert first == again
    with sessions() as db:
        db.info["user_id"] = user_id
        job = db.get(Job, first)
        assert qualification.dispatch(db, job).id == qualification.dispatch(db, job).id
        db.commit()
    qs.drive(sessions)
    assert len(qs.qualification_tasks(sessions, first)) == 1
    assert model.calls.count(first) == 1 and parses.parses.count(first) == 1


# ───────────── tenancy ─────────────

def test_another_account_can_neither_see_nor_stop_a_run(sessions, model):
    mine, theirs = add_owner(sessions), add_owner(sessions)
    job_id = qs.new_job(sessions, mine)
    qs.drive(sessions, passes=("qualification",), rounds=1, settle=False)
    task = qs.qualification_tasks(sessions, job_id)[-1]
    with sessions() as db:
        db.info["user_id"] = theirs
        assert db.get(JobQualificationTask, task.id) is None
        assert workers.rows(db, theirs, "qualification")["rows"] == []
        with pytest.raises(workers.WorkerError):
            workers.kill(db, theirs, "qualification", [task.id])
        from app.agents import threads

        with pytest.raises((threads.ThreadNotFound, threads.ThreadOwnershipError)):
            threads.owned(db, task.agent_thread_id)
        with pytest.raises((threads.ThreadNotFound, threads.ThreadOwnershipError, runtime.ThreadBusy)):
            runtime.resume(db, task.agent_thread_id, {"action": "continue"}, "intruder", by_worker=True)
    assert qs.qualification_tasks(sessions, job_id)[-1].status == "PENDING"
    with sessions() as db:
        db.info["user_id"] = mine
        assert [r["task_id"] for r in workers.rows(db, mine, "qualification")["rows"]] == [task.id]


def test_a_person_cannot_resume_a_worker_owned_run_through_the_api(sessions, model):
    user_id = add_owner(sessions)
    job_id = qs.new_job(sessions, user_id)
    qs.drive(sessions, passes=("qualification",), rounds=1, settle=False)
    task = qs.qualification_tasks(sessions, job_id)[-1]
    with sessions() as db:
        db.info["user_id"] = user_id
        with pytest.raises(runtime.ThreadBusy):
            runtime.resume(db, task.agent_thread_id, {"action": "continue"}, "api")
    with sessions() as db:  # the generic recovery pass leaves worker-owned threads alone
        db.execute(update(AgentThread).values(status="RUNNING", lock_expires_at=qs.utcnow()))
        db.commit()
    assert runtime.claim_stale(sessions, "recovery", 10) == []


# ───────────── orchestration keeps its factual semantics ─────────────

def test_orchestration_counts_the_same_facts_for_both_paths(sessions, monkeypatch, model, parses, server):
    from datetime import timedelta

    accounts = {}

    def build(user_id):
        accounts.setdefault("users", []).append(user_id)
        ids = [qs.new_job(sessions, user_id), qs.new_job(sessions, user_id, title="Account Executive"),
               qs.new_job(sessions, user_id, title="Platform Engineering Manager", description=qs.STUB,
                          url=server.url("posting.html"))]
        model.answer(ids[0], {"overall_fit_score": 40})
        return ids[0]

    both(sessions, monkeypatch, build, browser=FakeBrowser())
    legacy_user, graph_user = accounts["users"]
    now = qs.utcnow()
    with sessions() as db:
        views = []
        for user_id in (legacy_user, graph_user):
            db.info["user_id"] = user_id
            scope = orchestration.Scope(viewer_id=user_id, user_id=user_id, start=now - timedelta(days=1),
                                        end=now + timedelta(minutes=1), range_name="custom")
            built = orchestration._build_graph(db, scope)
            edges = {k: v.get("count") for k, v in built["edges"].items()} if isinstance(built["edges"], dict) \
                else {e["id"]: e.get("count") for e in built["edges"]}
            nodes = {n: (built["nodes"][n]["tasks"] or {}).get("completed") for n in ("jd_recovery", "jd_parser",
                                                                                     "ranker")}
            live = {k: v for k, v in orchestration.live_counts(db, user_id, now).items() if k != "qualification"}
            views.append((edges, nodes, live))
    assert views[0] == views[1]
    assert orchestration.topo.validate() == []


# ───────────── shadow: compares, never writes, never pays by default ─────────────

def test_shadow_runs_compare_with_the_current_path_without_writing_or_calling_a_model(sessions, monkeypatch, model,
                                                                                     parses):
    qs.set_mode(monkeypatch, "shadow")
    user_id = add_owner(sessions)
    qualified, archived = qs.new_job(sessions, user_id), qs.new_job(sessions, user_id)
    hidden = qs.new_job(sessions, user_id, title="Account Executive")
    model.answer(archived, {"overall_fit_score": 30})
    qs.drive(sessions, passes=("jd_recovery", "jd_parsing", "ranking"))  # the current path decides
    before = {j: qs.facts(sessions, j) for j in (qualified, archived, hidden)}
    calls = list(model.calls)
    qs.drive(sessions, passes=("qualification",))
    assert {j: qs.facts(sessions, j) for j in (qualified, archived, hidden)} == before  # nothing written
    assert model.calls == calls  # no paid call
    by_job = {t.job_id: t for j in (qualified, archived, hidden) for t in qs.qualification_tasks(sessions, j)}
    assert all(t.mode == "SHADOW" and t.status == "COMPLETED" and t.parity["match"] for t in by_job.values())
    assert by_job[qualified].outcome == "QUALIFIED" and by_job[archived].outcome == "ARCHIVED"
    assert by_job[hidden].outcome == "HIDDEN" and by_job[hidden].parity["hidden_zero_work"] is True
    assert all(t.paid_calls == 0 for t in by_job.values())


def test_shadow_runs_keep_to_the_daily_budget_and_pay_only_when_allowed(sessions, monkeypatch, model, parses):
    from app.models import JobRanking
    from app.services import server_settings

    qs.set_mode(monkeypatch, "shadow")
    limits = {"qualification_shadow_daily_limit": 2, "qualification_shadow_paid_daily_limit": 1}
    real = server_settings.value
    monkeypatch.setattr(server_settings, "value", lambda name: limits.get(name, real(name)))
    user_id = add_owner(sessions)
    jobs = [qs.new_job(sessions, user_id) for _ in range(3)]
    qs.drive(sessions, passes=("jd_recovery", "jd_parsing", "ranking"))
    with sessions() as db:  # a decided job whose ranking is gone: only a model call could predict it
        db.execute(update(JobRankingTask).values(ranking_id=None))
        db.query(JobRanking).delete()
        db.commit()
    calls = len(model.calls)
    qs.drive(sessions, passes=("qualification",))
    shadow = [t for j in jobs for t in qs.qualification_tasks(sessions, j)]
    assert len(shadow) == 2  # the account's daily limit
    assert sum(t.paid_calls for t in shadow) == 1 and len(model.calls) == calls + 1  # the paid limit
    unpaid = next(t for t in shadow if t.paid_calls == 0)
    assert unpaid.outcome == "UNAVAILABLE" and "decision" not in unpaid.parity["fields"]


# ───────────── mode off: nothing new runs, leftovers go back ─────────────

def test_mode_off_creates_no_runs_and_hands_leftover_runs_back(sessions, monkeypatch, model, parses):
    user_id = add_owner(sessions)
    waiting = qs.new_job(sessions, user_id)
    qs.drive(sessions, passes=("qualification",), rounds=1, settle=False)
    qs.set_mode(monkeypatch, "off")
    fresh = qs.new_job(sessions, user_id)
    assert qs.qualification_tasks(sessions, fresh) == []
    qs.drive(sessions)
    task = qs.qualification_tasks(sessions, waiting)[-1]
    assert (task.status, task.result_code) == ("SKIPPED", qualification.ROUTED_TO_LEGACY)
    assert qs.facts(sessions, waiting)["state"] == qs.facts(sessions, fresh)["state"] == "QUALIFIED"
    assert model.calls.count(waiting) == model.calls.count(fresh) == 1


# ───────────── pure parts (any database) ─────────────

def test_parity_compares_only_what_both_sides_know():
    same = qualification.parity({"predicted_admission": "MATCH", "actual_admission": "MATCH",
                                 "predicted_decision": "QUALIFIED", "actual_decision": "QUALIFIED",
                                 "predicted_filter": "FILTERED", "actual_filter": "FILTERED"})
    assert same["match"] and set(same["fields"]) == {"admission", "decision", "filter"}
    differs = qualification.parity({"predicted_decision": "ARCHIVED", "actual_decision": "QUALIFIED"})
    assert not differs["match"]
    unknown = qualification.parity({"predicted_decision": "UNAVAILABLE", "actual_decision": "QUALIFIED"})
    assert unknown["match"] and unknown["fields"] == {}
    leaked = qualification.parity({"predicted_admission": "NOT_MATCHING", "actual_admission": "NOT_MATCHING",
                                   "actual_tasks": 1})
    assert leaked["hidden_zero_work"] is False and not leaked["match"]


def test_graph_state_exported_is_ids_enums_and_counts_only():
    assert graph.EXPORT_FIELDS and all(not any(w in f for w in ("description", "resume_text", "prompt", "email"))
                                       for f in graph.EXPORT_FIELDS)
    assert graph.summary({"job_id": 1, "outcome": "QUALIFIED", "description": "x" * 50}) == {"job_id": 1,
                                                                                            "outcome": "QUALIFIED"}
    assert runtime.WORKER_OWNED == (graph.GRAPH,)
