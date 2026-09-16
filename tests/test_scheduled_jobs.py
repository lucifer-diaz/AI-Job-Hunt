"""Scheduled job services, the scheduler API, and the work-level guarantees behind them.

Ranking and cleanup need PostgreSQL (TEST_DATABASE_URL); the rest also run on a throwaway SQLite database.
Work that belongs to a person runs per account, with that account's keys and preferences.
"""
import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.db import AppSession, SessionLocal
from app.db_migrations import run_migrations
from app.models import CandidateProfile, Job, SchedulerJob, SearchRun, WorkflowTransition
from app.scheduler import Scheduler
from app.services import scheduled_jobs as sj
from app.services import work_queue as wq
from app.services.compensation import extract_posting_salary, parse_salary_text
from app.services.lifecycle import WorkflowState as S
from app.services.llm import LLMError
from app.services.user_settings import apify_token, set_user_keys, settings_for
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(main.app)
ADMIN = {"X-Admin-Token": "s3cret"}


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def sqlite_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    run_migrations(engine)
    yield sessionmaker(bind=engine, class_=AppSession)
    engine.dispose()


@pytest.fixture
def owner(pg_sessions):
    return make_user(pg_sessions)


def ctx(sessions, trigger="schedule", interval=5, worker=None):
    return sj.JobContext(sessions=sessions, trigger=trigger, now=utcnow(), worker_id=worker or f"test:{uuid.uuid4().hex[:6]}",
                         interval_minutes=interval)


def add_jobs(sessions, count, user_id, **fields):
    with sessions() as db:
        jobs = [Job(**{"user_id": user_id, "source": "test", "source_ref": uuid.uuid4().hex, "title": f"Role {i}",
                       "company": "Acme", **fields}) for i in range(count)]
        db.add_all(jobs)
        db.commit()
        return [job.id for job in jobs]


def use_provider(sessions, user_id, provider, **preferences):
    with sessions() as db:
        row = settings_for(db, user_id)
        row.preferences = {**(row.preferences or {}), "ai": {"llm_provider": provider}, **preferences}
        db.commit()


@pytest.fixture
def fake_ranker(monkeypatch):
    """A slow, deterministic stand-in for LLM ranking that records concurrency and can fail on demand."""
    state = {"active": 0, "peak": 0, "calls": [], "users": set(), "fail": set()}
    lock = threading.Lock()

    def rank(db, job):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"].append(job.id)
            state["users"].add(db.info.get("user_id"))
        try:
            time.sleep(0.05)
            if job.id in state["fail"]:
                raise LLMError("provider timed out")
            job.fit_score, job.fit_analysis = 80.0, {"score": 80}
            db.commit()
            return {"score": 80}
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(sj, "rank_job", rank)
    return state


# ───────────── ranking (PostgreSQL) ─────────────

def test_ranking_claims_a_limited_batch_and_limits_concurrency(pg_sessions, owner, fake_ranker, monkeypatch):
    monkeypatch.setattr(get_settings(), "ranking_batch_size", 12)
    monkeypatch.setattr(get_settings(), "ranking_concurrency", 3)
    ids = add_jobs(pg_sessions, 20, owner)

    result = asyncio.run(sj.ranking(ctx(pg_sessions)))
    assert result["claimed"] == 12 and result["ranked"] == 12
    assert 1 < fake_ranker["peak"] <= 3
    assert fake_ranker["users"] == {owner}  # each job is ranked in a session bound to its owner
    with pg_sessions() as db:
        assert dict(db.execute(select(Job.state, func.count()).group_by(Job.state)).all()) == {S.RANKED: 12, S.DISCOVERED: 8}
        steps = db.scalars(select(WorkflowTransition).where(WorkflowTransition.job_id == ids[0])
                           .order_by(WorkflowTransition.id)).all()
        assert [(t.from_state, t.to_state) for t in steps] == [
            (S.DISCOVERED, S.NORMALIZED), (S.NORMALIZED, S.FILTERED), (S.FILTERED, S.RANKED)]

    assert asyncio.run(sj.ranking(ctx(pg_sessions)))["ranked"] == 8
    assert asyncio.run(sj.ranking(ctx(pg_sessions)))["claimed"] == 0  # running again is harmless
    assert sorted(fake_ranker["calls"]) == ids


def test_ranking_only_claims_jobs_of_accounts_that_want_it_and_can_pay_for_it(pg_sessions, fake_ranker):
    ready, auto_rank_off, no_key = make_user(pg_sessions), make_user(pg_sessions), make_user(pg_sessions)
    with pg_sessions() as db:
        db.add(CandidateProfile(user_id=auto_rank_off, search_preferences={"search_profile": {"auto_rank": False}}))
        db.commit()
    use_provider(pg_sessions, no_key, "openrouter")
    ranked_ids = add_jobs(pg_sessions, 2, ready)
    untouched = add_jobs(pg_sessions, 1, auto_rank_off) + add_jobs(pg_sessions, 1, no_key)

    result = asyncio.run(sj.ranking(ctx(pg_sessions)))
    assert result["claimed"] == 2 and sorted(fake_ranker["calls"]) == ranked_ids
    with pg_sessions() as db:
        assert all(db.get(Job, job_id).state is S.DISCOVERED and db.get(Job, job_id).attempt_count == 0
                   for job_id in untouched)

    with pg_sessions() as db:  # once the account saves its key, its jobs are picked up
        set_user_keys(db, {"openrouter": "sk-or-v1-account"}, user_id=no_key)
    assert asyncio.run(sj.ranking(ctx(pg_sessions)))["claimed"] == 1


def test_each_ranking_result_is_saved_independently(pg_sessions, owner, fake_ranker):
    good, broken, other = add_jobs(pg_sessions, 3, owner)
    fake_ranker["fail"].add(broken)
    result = asyncio.run(sj.ranking(ctx(pg_sessions)))
    assert result["ranked"] == 2 and result["retrying"] == 1
    with pg_sessions() as db:
        assert db.get(Job, good).state is S.RANKED and db.get(Job, other).state is S.RANKED
        job = db.get(Job, broken)
        assert job.state is S.FILTERED and job.attempt_count == 1 and job.retryable is True
        assert job.next_attempt_at is not None and "provider timed out" in job.last_error and job.lock_token is None


def test_two_app_instances_do_not_rank_the_same_job(pg_sessions, owner, fake_ranker, monkeypatch):
    monkeypatch.setattr(get_settings(), "ranking_batch_size", 5)
    ids = add_jobs(pg_sessions, 30, owner)
    barrier = threading.Barrier(3)

    def instance(n):
        barrier.wait()
        ranked = 0
        while (out := asyncio.run(sj.ranking(ctx(pg_sessions, worker=f"instance-{n}"))))["claimed"]:
            ranked += out["ranked"]
        return ranked

    with ThreadPoolExecutor(3) as pool:
        per_instance = list(pool.map(instance, range(3)))
    assert sum(per_instance) == 30
    assert sorted(fake_ranker["calls"]) == ids  # every job ranked exactly once across instances


def test_restart_picks_up_work_left_by_a_crashed_instance(pg_sessions, owner, fake_ranker):
    crashed_ids = add_jobs(pg_sessions, 3, owner)
    waiting_ids = add_jobs(pg_sessions, 2, owner)
    long_ago = utcnow() - timedelta(hours=2)
    with pg_sessions() as db:
        claims = wq.claim(db, [S.DISCOVERED], worker_id="crashed-instance", limit=3, lease_seconds=60, now=long_ago)
        # The crashed instance also held the scheduler's lease for the ranking job.
        db.add(SchedulerJob(name="ranking", locked_by="crashed-instance:run", locked_at=long_ago,
                            lock_expires_at=long_ago + timedelta(minutes=30)))
        db.commit()
    assert [c.job_id for c in claims] == crashed_ids

    restarted = Scheduler([s for s in sj.JOBS if s.name == "ranking"], pg_sessions, intervals={"ranking": 2},
                          lease_seconds=600, instance_id="restarted")
    outcome = asyncio.run(restarted.run_job("ranking", "schedule"))
    assert outcome.status == "succeeded" and outcome.result["ranked"] == 5
    with pg_sessions() as db:
        for job_id in crashed_ids:
            job = db.get(Job, job_id)
            assert job.state is S.RANKED and job.lock_token is None and job.last_error is None
    assert sorted(fake_ranker["calls"]) == crashed_ids + waiting_ids


def test_ranking_filters_before_ranking_and_does_not_rerank(pg_sessions, owner, fake_ranker):
    with pg_sessions() as db:
        db.add(CandidateProfile(user_id=owner, search_preferences={"exclude_companies": ["Evil Corp"]}))
        db.commit()
    dismissed, = add_jobs(pg_sessions, 1, owner, status="dismissed")
    excluded, = add_jobs(pg_sessions, 1, owner, company="Evil Corp")
    already_ranked, = add_jobs(pg_sessions, 1, owner, fit_score=91.0)

    result = asyncio.run(sj.ranking(ctx(pg_sessions)))
    assert result["filtered_out"] == 2 and result["ranked"] == 1 and fake_ranker["calls"] == []
    with pg_sessions() as db:
        assert db.get(Job, dismissed).state is S.SKIPPED and db.get(Job, excluded).state is S.SKIPPED
        assert db.get(Job, already_ranked).state is S.RANKED
        reason = db.scalars(select(WorkflowTransition.reason).where(WorkflowTransition.job_id == excluded)
                            .order_by(WorkflowTransition.id.desc())).first()
        assert "excluded" in reason


def test_salary_enrichment_never_blocks_the_ranking_claim(pg_sessions, owner, fake_ranker, monkeypatch):
    """Both jobs start together on boot; enrichment must not hold locks that make ranking skip jobs."""
    ids = add_jobs(pg_sessions, 6, owner, raw={"salary": "$150k-$180k a year"})
    enriching = threading.Event()
    real_enrich = sj.enrich_compensation

    def slow_enrich(raw, now):
        enriching.set()
        time.sleep(0.1)
        return real_enrich(raw, now)

    monkeypatch.setattr(sj, "enrich_compensation", slow_enrich)
    with ThreadPoolExecutor(1) as pool:
        enrichment = pool.submit(sj.compensation, ctx(pg_sessions))
        assert enriching.wait(5)
        ranked = asyncio.run(sj.ranking(ctx(pg_sessions)))
        assert enrichment.result()["salary_found"] == 6
    assert ranked["claimed"] == 6 and ranked["ranked"] == 6
    assert sorted(fake_ranker["calls"]) == ids


def test_compensation_does_not_overwrite_a_job_checked_by_another_instance(pg_sessions, owner, monkeypatch):
    job_id, = add_jobs(pg_sessions, 1, owner, raw={"salary": "$150k"})
    real_enrich = sj.enrich_compensation

    def racing_enrich(raw, now):  # another instance finishes this job while we are still computing
        with pg_sessions() as db:
            db.get(Job, job_id).compensation_checked_at = now
            db.get(Job, job_id).compensation = {"status": "by-other-instance"}
            db.commit()
        return real_enrich(raw, now)

    monkeypatch.setattr(sj, "enrich_compensation", racing_enrich)
    assert sj.compensation(ctx(pg_sessions))["checked"] == 0
    with pg_sessions() as db:
        assert db.get(Job, job_id).compensation == {"status": "by-other-instance"}


def test_ranking_waits_while_no_account_has_an_ai_key(pg_sessions, owner):
    job_id, = add_jobs(pg_sessions, 1, owner)
    use_provider(pg_sessions, owner, "openrouter")
    assert asyncio.run(sj.ranking(ctx(pg_sessions))) == {"skipped": "No account has auto-ranking on with an AI key saved"}
    with pg_sessions() as db:
        assert db.get(Job, job_id).attempt_count == 0 and db.get(Job, job_id).state is S.DISCOVERED


def test_ranking_needs_postgresql(sqlite_sessions):
    add_jobs(sqlite_sessions, 1, make_user(sqlite_sessions))
    assert "PostgreSQL" in asyncio.run(sj.ranking(ctx(sqlite_sessions)))["skipped"]


def test_ai_ready_follows_each_accounts_provider_and_key(sqlite_sessions):
    account = make_user(sqlite_sessions)
    with sqlite_sessions() as db:
        assert sj.ai_ready(db, account) == (True, "")  # mock provider needs no key
    use_provider(sqlite_sessions, account, "openrouter")
    with sqlite_sessions() as db:
        assert sj.ai_ready(db, account) == (False, "No OpenRouter API key saved in Settings")
        set_user_keys(db, {"openrouter": "sk-or-v1-account"}, user_id=account)
        assert sj.ai_ready(db, account) == (True, "")


def test_cleanup_recovers_expired_leases(pg_sessions, owner):
    job_id, = add_jobs(pg_sessions, 1, owner)
    with pg_sessions() as db:
        wq.claim(db, [S.DISCOVERED], worker_id="gone", lease_seconds=60, now=utcnow() - timedelta(hours=1))
    assert sj.cleanup(ctx(pg_sessions)) == {"recovered_leases": 1}
    assert sj.cleanup(ctx(pg_sessions)) == {"recovered_leases": 0}
    with pg_sessions() as db:
        assert db.get(Job, job_id).lock_token is None


# ───────────── enrichment, hunter, sync, reports ─────────────

@pytest.mark.parametrize("text,expected", [
    ("$120,000 - $150,000 a year", {"min": 120000, "max": 150000, "currency": "USD", "period": "year"}),
    ("£55k - £65k per year", {"min": 55000, "max": 65000, "currency": "GBP", "period": "year"}),
    ("120-150k USD", {"min": 120000, "max": 150000, "currency": "USD", "period": None}),
    ("S$8,000 – S$10,000 per month", {"min": 8000, "max": 10000, "currency": "SGD", "period": "month"}),
    ("₹25,00,000 per annum", {"min": 2500000, "max": None, "currency": "INR", "period": "year"}),
    ("$45/hr", {"min": 45, "max": None, "currency": "USD", "period": "hour"}),
    ("Competitive", None),
    ("3 days a week in office", None),
])
def test_salary_text_parsing(text, expected):
    assert parse_salary_text(text) == expected


def test_posting_salary_from_structured_actor_fields():
    assert extract_posting_salary({"salary_raw": {"currency": "EUR", "value": {"minValue": 70000, "maxValue": 90000,
                                                                              "unitText": "YEAR"}}}) == \
        {"min": 70000, "max": 90000, "currency": "EUR", "period": "year", "field": "salary_raw"}
    assert extract_posting_salary({"ai_salary_minvalue": 5000, "ai_salary_currency": "SGD",
                                   "ai_salary_unittext": "MONTH"})["period"] == "month"
    assert extract_posting_salary({"description": "Grow revenue to $10,000,000"}) is None


def test_compensation_enrichment_runs_once_per_job(sqlite_sessions):
    account = make_user(sqlite_sessions)
    with sqlite_sessions() as db:
        db.add_all([
            Job(user_id=account, source="t", source_ref="a", title="A", company="X",
                raw={"min_amount": 120000, "max_amount": 150000, "currency": "USD", "interval": "yearly"}),
            Job(user_id=account, source="t", source_ref="b", title="B", company="Y", raw={"salary": "£55k - £65k per year"}),
            Job(user_id=account, source="t", source_ref="c", title="C", company="Z", raw={}),
        ])
        db.commit()
    assert sj.compensation(ctx(sqlite_sessions)) == {"checked": 3, "salary_found": 2, "remaining": 0}
    assert sj.compensation(ctx(sqlite_sessions))["checked"] == 0
    with sqlite_sessions() as db:
        job = db.scalar(select(Job).where(Job.source_ref == "a"))
        assert job.compensation["posting"] == {"min": 120000, "max": 150000, "currency": "USD", "period": "year",
                                               "field": "min_amount"}
        assert job.compensation["glassdoor"] == {"status": "not_configured"} and job.compensation_checked_at
        assert db.scalar(select(Job).where(Job.source_ref == "c")).compensation["status"] == "not_listed"


def test_hunter_runs_each_accounts_search_with_its_own_token(sqlite_sessions, monkeypatch):
    assert sj.hunter(ctx(sqlite_sessions)) == {"skipped": "No accounts yet"}
    searcher, _without_key = make_user(sqlite_sessions), make_user(sqlite_sessions)
    with sqlite_sessions() as db:
        set_user_keys(db, {"apify": "apify_api_searcher"}, user_id=searcher)
    profile = {"roles": ["Engineer"], "schedule": "manual"}
    monkeypatch.setattr(sj, "get_search_profile", lambda db: dict(profile))
    started = []

    def fake_run(db, sp):
        started.append((db.info["user_id"], apify_token(db)))
        run = SearchRun(external_run_id=uuid.uuid4().hex, actor_id="openclawai/job-board-scraper")
        db.add(run)  # stamped with the session's account
        db.commit()
        return [run]

    monkeypatch.setattr(sj, "run_search_profile", fake_run)
    manual = sj.hunter(ctx(sqlite_sessions, trigger="schedule"))
    assert manual["not_started"] == {"schedule is manual": 1, "no Apify key": 1} and started == []

    profile["schedule"] = "daily"
    out = sj.hunter(ctx(sqlite_sessions, trigger="schedule", interval=240))
    assert out["started"] == 1 and started == [(searcher, "apify_api_searcher")]
    again = sj.hunter(ctx(sqlite_sessions, trigger="manual", interval=240))
    assert again["skipped"] == "No account had a search due" and again["not_started"]["searched recently"] == 1
    with sqlite_sessions() as db:
        assert db.scalar(select(SearchRun.user_id)) == searcher and len(started) == 1


def test_apify_sync_imports_each_finished_run_once_with_the_owners_token(sqlite_sessions, monkeypatch):
    account, keyless = make_user(sqlite_sessions), make_user(sqlite_sessions)
    with sqlite_sessions() as db:
        set_user_keys(db, {"apify": "apify_api_account"}, user_id=account)
        db.add_all([SearchRun(user_id=account, external_run_id="runok", actor_id="a/b"),
                    SearchRun(user_id=account, external_run_id="runbad", actor_id="a/b"),
                    SearchRun(user_id=keyless, external_run_id="runkeyless", actor_id="a/b")])
        db.commit()

    def fake_refresh(db, run):
        if run.external_run_id == "runbad":
            raise RuntimeError("Apify returned 500")
        run.status, run.completed_at = "SUCCEEDED", utcnow()
        db.commit()
        return {"completed": True, "import": {"created": 4}}

    monkeypatch.setattr(sj, "refresh_apify_run", fake_refresh)
    first = sj.apify_sync(ctx(sqlite_sessions))
    assert (first["checked"], first["completed"], first["created"], first["no_token"]) == (1, 1, 4, 1)
    assert first["errors"] == [{"run_id": 2, "error": "Apify returned 500"}]
    second = sj.apify_sync(ctx(sqlite_sessions))
    assert second["checked"] == 0 and len(second["errors"]) == 1  # the finished run is not imported again


def test_follow_ups_run_per_account_and_reports(sqlite_sessions, monkeypatch):
    account = make_user(sqlite_sessions)
    add_jobs(sqlite_sessions, 2, account)
    add_jobs(sqlite_sessions, 1, account, state=S.READY_TO_APPLY)
    apps = sj.applications(ctx(sqlite_sessions))
    assert apps["ready_to_apply"] == 1 and "not enabled" in apps["skipped"]
    assert "email provider" in sj.inbox(ctx(sqlite_sessions))["skipped"]
    stats = sj.analytics(ctx(sqlite_sessions))
    assert stats["accounts"] == 1 and stats["jobs_by_state"] == {"DISCOVERED": 2, "READY_TO_APPLY": 1}
    assert stats["pending"] == {"ranking": 2, "compensation": 3, "search_runs": 0}
    assert "PostgreSQL" in sj.cleanup(ctx(sqlite_sessions))["skipped"]

    keyless = make_user(sqlite_sessions)
    use_provider(sqlite_sessions, keyless, "openai")
    drafted_for = []
    monkeypatch.setattr(sj, "create_due_followups", lambda db: drafted_for.append(db.info["user_id"]) or 2)
    assert sj.followups(ctx(sqlite_sessions)) == {"accounts": 2, "drafted": 2, "no_ai_key": 1, "errors": []}
    assert drafted_for == [account]


# ───────────── FastAPI integration ─────────────

def test_scheduler_starts_and_stops_with_the_app(monkeypatch):
    monkeypatch.setattr(main.scheduler, "enabled", True)
    monkeypatch.setattr(main.scheduler, "intervals", {})  # run the loop without starting real jobs
    assert not main.scheduler.running
    with TestClient(main.app) as live:
        status = live.get("/api/system/scheduler").json()
        assert status["enabled"] is True and status["running"] is True and status["started_at"]
        assert [job["name"] for job in status["jobs"]] == list(main.scheduler.specs)
    assert not main.scheduler.running


def test_disabled_scheduler_does_not_start():
    with TestClient(main.app) as live:
        status = live.get("/api/system/scheduler").json()
    assert status["enabled"] is False and status["running"] is False
    assert all(job["enabled"] is False for job in status["jobs"])


def test_manual_triggers_are_protected_and_share_the_scheduler_lease(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_token", None)
    assert client.post("/api/admin/scheduler/analytics/run").status_code == 403
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    assert client.post("/api/admin/scheduler/analytics/run", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.post("/api/admin/scheduler/analytics/run").status_code == 401
    assert client.post("/api/admin/scheduler/nope/run", headers=ADMIN).status_code == 404

    out = client.post("/api/admin/scheduler/compensation/run", headers=ADMIN)
    assert out.status_code == 200 and out.json()["status"] == "succeeded" and out.json()["trigger"] == "manual"
    status = {job["name"]: job for job in client.get("/api/system/scheduler").json()["jobs"]}
    assert status["compensation"]["last_trigger"] == "manual" and status["compensation"]["run_count"] >= 1
    assert client.post("/api/admin/scheduler/apify-sync/run", headers=ADMIN).json()["job"] == "apify_sync"

    with SessionLocal() as db:  # another instance is running the inbox job right now
        db.merge(SchedulerJob(name="inbox", locked_by="other-host:1:abc:run", locked_at=utcnow(),
                              lock_expires_at=utcnow() + timedelta(minutes=10)))
        db.commit()
    try:
        busy = client.post("/api/admin/scheduler/inbox/run", headers=ADMIN)
        assert busy.status_code == 409 and "other-host" in busy.json()["detail"]
    finally:
        with SessionLocal() as db:
            db.merge(SchedulerJob(name="inbox", locked_by=None, locked_at=None, lock_expires_at=None))
            db.commit()
