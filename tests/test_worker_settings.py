"""Running workers → Settings: an account's own worker settings, and the server's (admin only)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import (
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
    ServerSetting,
    TailorTask,
)
from app.scheduler import build_scheduler
from app.services import jd_recovery, job_description, ranker, server_settings, tailor_tasks, worker_settings
from app.services.lifecycle import ErrorCategory
from app.settings import get_settings
from tests.conftest import make_user


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clean_server_settings():
    def clear():
        with SessionLocal() as db:
            db.execute(delete(ServerSetting))
            db.commit()
        server_settings.reset_cache()
    clear()
    yield
    clear()


@pytest.fixture
def user_id():
    return make_user(SessionLocal)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid, admin=False):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", admin))
        return TestClient(app)
    return sign_in


def add_job(db, uid):
    job = Job(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Staff Engineer", company="Canva",
              description="Build Python services. " * 20, url=f"https://example.com/{uuid.uuid4().hex}")
    db.add(job)
    db.flush()
    return job


def configure(uid, **workers):
    with SessionLocal() as db:
        worker_settings.update(db, uid, workers)


# ───────────── an account's own settings ─────────────

def test_defaults_follow_the_server_and_saving_the_default_stores_nothing(user_id, signed_in):
    client = signed_in(user_id)
    out = client.get("/api/workers/settings").json()
    settings = get_settings()
    assert out["account"]["parsing"]["values"] == {"auto": True, "max_attempts": settings.jd_parse_max_attempts,
                                                   "retry_seconds": settings.jd_parse_retry_seconds}
    assert out["account"]["recovery"]["values"]["use_browser"] is True
    assert out["server_editable"] is False
    assert set(out["server"]) == set(server_settings.EDITABLE)
    saved = client.put("/api/workers/settings", json={"workers": {"parsing": {"max_attempts": 5, "auto": False},
                                                                  "recovery": {"use_model": False}}}).json()
    assert saved["account"]["parsing"]["values"]["max_attempts"] == 5
    assert saved["account"]["parsing"]["values"]["auto"] is False
    assert saved["account"]["recovery"]["values"]["use_model"] is False
    client.put("/api/workers/settings", json={"workers": {"parsing": {"max_attempts": settings.jd_parse_max_attempts,
                                                                      "auto": True}}}).raise_for_status()
    with SessionLocal() as db:
        from app.services.user_settings import settings_for

        assert settings_for(db, user_id).preferences["workers"]["parsing"] == {}  # back to following the server
    assert client.put("/api/workers/settings", json={"workers": {"parsing": {"max_attempts": 99}}}).status_code == 422
    assert client.put("/api/workers/settings", json={"workers": {"parsing": {"bogus": 1}}}).status_code == 422
    assert client.put("/api/workers/settings", json={"workers": {"nope": {"auto": True}}}).status_code == 422
    assert client.get("/api/workers").json()["workers"][0]["paused"] is False


def test_a_paused_queue_keeps_its_tasks_until_run_now(user_id):
    other = make_user(SessionLocal)
    configure(user_id, parsing={"auto": False}, recovery={"auto": False})
    with SessionLocal() as db:
        mine = JobDescriptionParseTask(user_id=user_id, job_id=add_job(db, user_id).id, description_hash="h",
                                       status="PENDING", attempt_count=0)
        pushed = JobDescriptionParseTask(user_id=user_id, job_id=add_job(db, user_id).id, description_hash="h",
                                         status="PENDING", attempt_count=0, run_now_at=utcnow())
        theirs = JobDescriptionParseTask(user_id=other, job_id=add_job(db, other).id, description_hash="h",
                                         status="PENDING", attempt_count=0)
        fetch = JobDescriptionFetchTask(user_id=user_id, job_id=add_job(db, user_id).id, status="PENDING",
                                        attempt_count=0)
        db.add_all([mine, pushed, theirs, fetch])
        db.commit()
        ids = mine.id, pushed.id, theirs.id, fetch.id
    with SessionLocal() as db:
        claimed = {c.task_id for c in job_description.claim(db, worker_id="w", limit=500)}
        fetched = {c.task_id for c in jd_recovery.claim(db, worker_id="w", limit=500)}
    assert ids[0] not in claimed and ids[1] in claimed and ids[2] in claimed
    assert ids[3] not in fetched


def test_a_failed_parse_uses_the_accounts_attempts_and_retry_wait(user_id):
    configure(user_id, parsing={"max_attempts": 4, "retry_seconds": 50})
    now = utcnow()
    with SessionLocal() as db:
        task = JobDescriptionParseTask(user_id=user_id, job_id=add_job(db, user_id).id, description_hash="h",
                                       status="PROCESSING", attempt_count=2, lock_token="t")
        db.add(task)
        db.commit()
        claim = job_description.ParseClaim(task.id, "t", "w")
        assert job_description._failed(db, claim, RuntimeError("boom"), now, config=False,
                                       category=ErrorCategory.EXTERNAL_SERVICE) == "retrying"
        db.refresh(task)
        assert task.next_attempt_at == now + timedelta(seconds=100)
    configure(user_id, parsing={"max_attempts": 2})
    with SessionLocal() as db:
        task = db.get(JobDescriptionParseTask, claim.task_id)
        task.status, task.lock_token, task.attempt_count = "PROCESSING", "t", 2
        db.commit()
        assert job_description._failed(db, claim, RuntimeError("boom"), now, config=False,
                                       category=ErrorCategory.EXTERNAL_SERVICE) == "failed"


def test_recovery_carries_the_accounts_fallback_switches(user_id):
    configure(user_id, recovery={"use_browser": False, "use_model": False})
    with SessionLocal() as db:
        job = add_job(db, user_id)
        job.description = ""
        task = JobDescriptionFetchTask(user_id=user_id, job_id=job.id, status="PROCESSING", attempt_count=1,
                                       lock_token="t", reason="user_request")
        db.add(task)
        db.commit()
        claim = jd_recovery.FetchClaim(task.id, "t", "w", user_id, job.id)
    snapshot = jd_recovery._load(SessionLocal, claim)
    assert (snapshot.use_browser, snapshot.use_model) == (False, False)


def test_ranking_retries_follow_the_account(user_id):
    configure(user_id, ranking={"max_attempts": 5, "retry_base_seconds": 20, "retry_max_seconds": 60})
    assert ranker.retry_delay(1, 20, 60) == timedelta(seconds=20)
    assert ranker.retry_delay(4, 20, 60) == timedelta(seconds=60)
    assert ranker.retry_delay(1, 3600, 60) == timedelta(seconds=60)  # the longest wait is always the ceiling
    with SessionLocal() as db, pytest.raises(worker_settings.WorkerSettingError):
        worker_settings.update(db, user_id, {"ranking": {"retry_base_seconds": 3600}})  # longer than the longest
    now = utcnow()
    with SessionLocal() as db:
        task = JobRankingTask(user_id=user_id, job_id=add_job(db, user_id).id, status="PROCESSING", attempt_count=3,
                              reason="new")
        db.add(task)
        db.flush()
        assert ranker._retry_or_fail(db, task, None, ErrorCategory.TRANSIENT, "x", retryable=True, actor="w",
                                     now=now) == "retrying"
        assert task.next_attempt_at == now + timedelta(seconds=60)
        task.attempt_count = 5
        assert ranker._retry_or_fail(db, task, None, ErrorCategory.TRANSIENT, "x", retryable=True, actor="w",
                                     now=now) == "failed"
        db.rollback()


def test_paused_tailoring_waits_unless_run_now(user_id):
    configure(user_id, tailoring={"auto": False})
    with SessionLocal() as db:
        db.execute(delete(TailorTask))
        job = add_job(db, user_id)
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="queued", stage="queued", attempts=0))
        db.commit()
    assert tailor_tasks.claim_next(SessionLocal, "w", 900) is None
    with SessionLocal() as db:
        task = db.query(TailorTask).filter(TailorTask.user_id == user_id).one()
        task.run_now_at = utcnow()
        db.commit()
        task_id = task.id
    assert tailor_tasks.claim_next(SessionLocal, "w", 900) == task_id
    with SessionLocal() as db:
        db.execute(delete(TailorTask))
        db.commit()


# ───────────── the server's settings ─────────────

def test_only_an_admin_changes_the_servers_worker_settings(user_id, signed_in):
    assert signed_in(user_id).put("/api/workers/server-settings",
                                  json={"values": {"jd_parse_batch_size": 7}}).status_code == 403
    admin = signed_in(user_id, admin=True)
    assert admin.get("/api/workers/settings").json()["server_editable"] is True
    out = admin.put("/api/workers/server-settings", json={"values": {"jd_parse_batch_size": 7,
                                                                     "ranking_interval_minutes": 9}}).json()
    assert out["server"]["jd_parse_batch_size"] == {**out["server"]["jd_parse_batch_size"], "value": 7,
                                                    "overridden": True}
    assert server_settings.value("jd_parse_batch_size") == 7
    scheduler = build_scheduler(SessionLocal)
    assert scheduler.interval("ranking") == 9
    assert build_scheduler(SessionLocal, intervals={"ranking": 3}).interval("ranking") == 3  # explicit wins
    default = get_settings().jd_parse_batch_size
    admin.put("/api/workers/server-settings", json={"values": {"jd_parse_batch_size": default}}).raise_for_status()
    assert server_settings.describe()["jd_parse_batch_size"]["overridden"] is False
    assert admin.put("/api/workers/server-settings", json={"values": {"jd_parse_batch_size": 0}}).status_code == 422
    assert admin.put("/api/workers/server-settings", json={"values": {"secret_key": 1}}).status_code == 422


def test_the_account_ceiling_follows_the_servers_override(user_id, signed_in):
    from app.services import user_settings

    signed_in(user_id, admin=True).put("/api/workers/server-settings",
                                       json={"values": {"ranking_concurrency": 2}}).raise_for_status()
    assert user_settings.processing_maximums()["ranking_concurrency"] == 2


def test_an_admin_can_raise_the_tailoring_ceiling(user_id, signed_in):
    from app.services import user_settings, workers

    signed_in(user_id, admin=True).put("/api/workers/server-settings",
                                       json={"values": {"tailor_concurrency": 4}}).raise_for_status()
    assert user_settings.processing_maximums()["tailoring_concurrency"] == 4
    assert workers.scheduler_facts(None, None)["concurrency"] == 4
    assert tailor_tasks.build_worker(SessionLocal).concurrency == 4
    assert tailor_tasks.TailorWorker(SessionLocal, concurrency=1).concurrency == 1  # a fixed limit stays fixed


def test_a_running_pass_follows_changed_limits_without_a_restart(monkeypatch):
    import asyncio

    from app.services import scheduled_jobs

    limits = {"jd_parse_concurrency": 1, "jd_parse_batch_size": 6}
    monkeypatch.setattr(server_settings, "value", lambda name: limits[name])
    monkeypatch.setattr(scheduled_jobs, "LIVE_CHECK_SECONDS", 0.01)
    queue, active, peaks = list(range(10)), 0, []

    def claim(n):
        taken, queue[:] = queue[:n], queue[n:]
        return taken

    async def process(task):
        nonlocal active
        active += 1
        peaks.append(active)
        if task == 0:
            limits["jd_parse_concurrency"] = 3  # an admin raises "side by side" while the first task runs
        await asyncio.sleep(0.05)
        active -= 1
        return "parsed"

    claimed, outcomes = asyncio.run(scheduled_jobs.run_live(concurrency="jd_parse_concurrency",
                                                            batch="jd_parse_batch_size", claim=claim, process=process))
    assert claimed == 6 and outcomes == {"parsed": 6}  # tasks per run caps the pass; the rest wait for the next
    assert peaks[0] == 1 and max(peaks) == 3  # the new limit applied mid-run
    assert queue == [6, 7, 8, 9]  # never claimed early: they are not shown as running while they wait


def test_a_pass_never_waits_for_a_stopped_task_and_keeps_running_ones_leased(monkeypatch, user_id):
    import asyncio
    import time

    from app.services import scheduled_jobs

    monkeypatch.setattr(server_settings, "value", lambda name: {"c": 3, "b": 3}[name])
    monkeypatch.setattr(scheduled_jobs, "LIVE_CHECK_SECONDS", 0.01)
    queue = [0, 1]

    def claim(n):
        taken, queue[:] = queue[:n], queue[n:]
        return [type("Claim", (), {"task_id": t})() for t in taken]

    async def process(c):
        await asyncio.sleep(5 if c.task_id == 0 else 0.02)  # 0: a model call that is still going when it is stopped
        return "parsed"

    started = time.monotonic()
    claimed, outcomes = asyncio.run(scheduled_jobs.run_live(concurrency="c", batch="b", claim=claim, process=process,
                                                            heartbeat=lambda claims: {c.task_id for c in claims} - {0}))
    assert time.monotonic() - started < 2  # the pass did not wait for the stopped call
    assert claimed == 2 and outcomes == {"parsed": 1, "stopped": 1}

    # The real heartbeat renews a running task's lease (a long model call never looks abandoned) and drops others.
    with SessionLocal() as db:
        mine = JobDescriptionParseTask(user_id=user_id, job_id=add_job(db, user_id).id, description_hash="h",
                                       status="PROCESSING", attempt_count=1, lock_token="t",
                                       lock_expires_at=utcnow() - timedelta(minutes=1))
        lost = JobDescriptionParseTask(user_id=user_id, job_id=add_job(db, user_id).id, description_hash="h",
                                       status="PROCESSING", attempt_count=1, lock_token="other")
        db.add_all([mine, lost])
        db.commit()
        ids = mine.id, lost.id
    beat = scheduled_jobs.renew(SessionLocal, JobDescriptionParseTask, 600)
    claims = [job_description.ParseClaim(ids[0], "t", "w"), job_description.ParseClaim(ids[1], "t", "w")]
    assert beat(claims) == {ids[0]}
    with SessionLocal() as db:
        assert db.get(JobDescriptionParseTask, ids[0]).lock_expires_at > utcnow() + timedelta(minutes=9)
