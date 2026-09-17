"""Scheduled job services, the scheduler API, and the work-level guarantees behind them.

Ranking and cleanup need PostgreSQL (TEST_DATABASE_URL); the rest also run on a throwaway SQLite database.
Work that belongs to a person runs per account, with that account's keys and preferences.
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.db import AppSession, SessionLocal
from app.db_migrations import run_migrations
from app.models import Job, SchedulerJob, SearchRun
from app.services import scheduled_jobs as sj
from app.services import work_queue as wq
from app.services.compensation import extract_posting_salary, parse_salary_text
from app.services.lifecycle import WorkflowState as S
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


# Ranking has its own tests: tests/test_ranker.py.


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
