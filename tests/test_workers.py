"""Running workers: listing every queue, killing (stop and requeue fresh) and running a task now.

Task rows are written directly, the way the workers write them, and the controls are exercised through the API. The
claims used here (JD parsing, JD recovery, tailoring) work on SQLite; nothing calls a real model or website.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import (
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
    ProviderCooldown,
    ResumeVariant,
    TailorTask,
    WorkflowTransition,
)
from app.services import backoff, external_api, jd_recovery, job_description, ranker, tailor_tasks, workers
from app.settings import get_settings
from tests.conftest import make_user


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clean_cooldown_cache():
    backoff.reset_cache()
    yield
    backoff.reset_cache()


@pytest.fixture
def user_id():
    return make_user(SessionLocal)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", False))
        return TestClient(app)
    return sign_in


def add_job(db, uid, **fields):
    values = dict(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Staff Engineer", company="Canva",
                  description="Build Python services. " * 20, url=f"https://example.com/{uuid.uuid4().hex}")
    values.update(fields)
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def parse_task(db, uid, job, **fields):
    task = JobDescriptionParseTask(**{"user_id": uid, "job_id": job.id, "description_hash": "h", "status": "PENDING",
                                      "attempt_count": 0, **fields})
    db.add(task)
    db.flush()
    return task


# ───────────── listing ─────────────

def test_every_worker_lists_its_own_tasks_with_counts(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        a, b, c = (add_job(db, user_id) for _ in range(3))
        parse_task(db, user_id, a, status="PROCESSING", started_at=utcnow(), lock_token="t", locked_by="w",
                   lock_expires_at=utcnow() + timedelta(minutes=5), attempt_count=1)
        parse_task(db, user_id, b, attempt_count=1, next_attempt_at=utcnow() + timedelta(minutes=2),
                   last_error=f"Timeout talking to sk-live-{'x' * 30}")
        db.add(TailorTask(user_id=user_id, job_id=c.id, status="queued", stage="queued", attempts=0))
        theirs = add_job(db, other, company="Hooli")
        parse_task(db, other, theirs)
        db.commit()
    client = signed_in(user_id)
    summary = client.get("/api/workers").json()
    kinds = {w["kind"]: w for w in summary["workers"]}
    assert list(kinds) == ["parsing", "recovery", "ranking", "insights", "tailoring", "reviewing"]
    assert kinds["parsing"]["counts"] == {"running": 1, "queued": 0, "retrying": 1, "active": 2}
    assert kinds["tailoring"]["counts"]["queued"] == 1 and summary["active"] == 3
    parsing = client.get("/api/workers/parsing").json()
    statuses = [r["status"] for r in parsing["rows"]]
    assert statuses == ["RUNNING", "RETRYING"]
    retry = parsing["rows"][1]
    assert retry["can_kill"] and retry["can_run_now"] and retry["next_attempt_at"]
    assert "sk-live" not in retry["last_error"]
    assert "Hooli" not in str(parsing) and "metrics" in parsing
    assert client.get("/api/workers/nothing").status_code == 404


def test_the_ranking_tab_keeps_the_tracker_facts(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="RANK_PENDING")
        db.add(JobRankingTask(user_id=user_id, job_id=job.id, status="PENDING"))
        db.commit()
    out = signed_in(user_id).get("/api/workers/ranking").json()
    assert out["tracker"]["counts"]["queued"] >= 1 and "interval_minutes" in out["tracker"]
    assert out["rows"][0]["queue_position"] == 1 and out["rows"][0]["status"] == "QUEUED"


# ───────────── kill ─────────────

def test_killing_a_running_parse_stops_it_for_good_and_the_old_run_cannot_write(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        task = parse_task(db, user_id, job, attempt_count=2)
        db.commit()
        task_id, job_id = task.id, job.id
    with SessionLocal() as db:
        [claim] = [c for c in job_description.claim(db, worker_id="w1", limit=50) if c.task_id == task_id]
    client = signed_in(user_id)
    out = client.post(f"/api/workers/parsing/tasks/{task_id}/kill").json()
    assert (out["stopped"], out["running"], out["cooldowns_cleared"]) == (1, 1, [])
    with SessionLocal() as db:
        task = db.get(JobDescriptionParseTask, task_id)
        assert (task.status, task.lock_token, task.next_attempt_at, task.last_error) == ("FAILED", None, None,
                                                                                         "Stopped by you")
        assert job_description._held(db, claim) is None  # whatever the old run finishes is discarded
        job_description.sweep(db)  # the automatic sweep does not queue it again
        assert db.query(JobDescriptionParseTask).filter_by(job_id=job_id, status="PENDING").count() == 0
    row = next(r for r in client.get("/api/workers/parsing").json()["rows"] if r["task_id"] == task_id)
    assert row["status"] == "STOPPED" and row["can_run_now"] and not row["can_kill"]
    # Retry brings it back when the person asks.
    assert client.post("/api/workers/parsing/retry", json={"task_ids": [task_id]}).json()["retried"]


def test_killing_a_running_ranking_takes_the_job_out_of_the_queue(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="RANKING", previous_state="RANK_PENDING")
        task = JobRankingTask(user_id=user_id, job_id=job.id, status="PROCESSING", attempt_count=2, lock_token="tok",
                              locked_by="w1", locked_at=utcnow(), lock_expires_at=utcnow() + timedelta(minutes=10),
                              started_at=utcnow())
        db.add(task)
        db.commit()
        job_id, task_id = job.id, task.id
    signed_in(user_id).post(f"/api/workers/ranking/tasks/{task_id}/kill").raise_for_status()
    with SessionLocal() as db:
        job, task = db.get(Job, job_id), db.get(JobRankingTask, task_id)
        assert job.state.value == "SKIPPED" and task.status == "SKIPPED" and task.last_error == "Stopped by you"
        moves = [m.to_state.value for m in db.scalars(select(WorkflowTransition).where(
            WorkflowTransition.job_id == job_id).order_by(WorkflowTransition.id)).all()]
        assert moves[-2:] == ["RANK_PENDING", "SKIPPED"]  # recorded workflow moves, by the person
        claim = ranker.TaskClaim(task_id, job_id, user_id, "tok", "w1", 2, False, utcnow())
        with pytest.raises(ranker.LeaseLost):
            ranker._owned(db, claim)


def test_kill_all_clears_the_accounts_cooldowns_and_kill_one_does_not(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        task = parse_task(db, user_id, job, attempt_count=1, next_attempt_at=utcnow() + timedelta(hours=1))
        db.add(ProviderCooldown(user_id=user_id, provider="OPENROUTER", until=utcnow() + timedelta(minutes=5),
                                consecutive=3, calls_skipped=0))
        db.commit()
        task_id = task.id
    client = signed_in(user_id)
    assert client.post(f"/api/workers/parsing/tasks/{task_id}/kill").json()["cooldowns_cleared"] == []
    assert len(client.get("/api/workers").json()["cooldowns"]) == 1
    out = client.post("/api/workers/parsing/kill-all").json()
    assert out["cooldowns_cleared"] == ["OPENROUTER"]
    assert client.get("/api/workers").json()["cooldowns"] == []
    assert client.post("/api/workers/kill-all").json()["cooldowns_cleared"] == []


def test_a_task_waiting_its_first_turn_has_nothing_to_kill(user_id, signed_in):
    with SessionLocal() as db:
        task = parse_task(db, user_id, add_job(db, user_id))
        done = parse_task(db, user_id, add_job(db, user_id), status="COMPLETED", completed_at=utcnow())
        db.commit()
        task_id, done_id = task.id, done.id
    client = signed_in(user_id)
    assert client.post(f"/api/workers/parsing/tasks/{task_id}/kill").json()["stopped"] == 0
    assert client.post(f"/api/workers/parsing/tasks/{done_id}/kill").status_code == 409


def test_a_killed_tailoring_run_stops_for_good_and_can_never_save(user_id, signed_in, monkeypatch):
    with SessionLocal() as db:
        db.execute(delete(TailorTask))  # the claim takes the oldest queued task of any account
        job = add_job(db, user_id, status="shortlisted", title="Original title")
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="queued", stage="queued", attempts=0))
        db.commit()
        job_id = job.id
    task_id = tailor_tasks.claim_next(SessionLocal, "worker-1", 900)
    with SessionLocal() as db:
        first_holder = db.get(TailorTask, task_id).locked_by
    assert first_holder.startswith("worker-1#")
    client = signed_in(user_id)
    client.post(f"/api/workers/tailoring/tasks/{task_id}/kill").raise_for_status()
    with SessionLocal() as db:
        task = db.get(TailorTask, task_id)
        assert (task.status, task.error, task.locked_by) == ("cancelled", "Stopped by you", None)
        assert db.get(Job, job_id).auto_tailor_status == "DECLINED"  # automatic tailoring leaves it alone
    assert tailor_tasks.claim_next(SessionLocal, "worker-1", 900) is None  # stopped: not picked up again
    assert tailor_tasks._write(SessionLocal, task_id, first_holder, stage="tailoring") is False

    # Run again on request; then stop it while the model "runs": its save is refused inside the transaction itself,
    # even without a progress checkpoint in between.
    fresh = client.post(f"/api/workers/tailoring/tasks/{task_id}/run-now").json()["task_id"]
    assert tailor_tasks.claim_next(SessionLocal, "worker-1", 900) == fresh

    def tailor(db, job, progress, pages=None):  # stands in for workflow.tailor_job: the model call, then the save
        client.post(f"/api/workers/tailoring/tasks/{fresh}/kill").raise_for_status()  # stopped mid-run
        job.title = "Changed by a stopped run"
        db.commit()
    monkeypatch.setattr(tailor_tasks, "tailor_job", tailor)
    assert tailor_tasks.run_task(SessionLocal, fresh, "worker-1", 900) == "abandoned"
    with SessionLocal() as db:
        assert db.get(Job, job_id).title == "Original title"
        assert db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)) is None
        assert db.get(TailorTask, fresh).status == "cancelled"
        assert db.get(TailorTask, fresh).run_now_at is None  # the run-now marker was used once and cleared
        db.execute(delete(TailorTask))
        db.commit()


# ───────────── run now ─────────────

def test_run_now_jumps_the_queue_and_skips_the_retry_wait(user_id, signed_in):
    with SessionLocal() as db:
        first = parse_task(db, user_id, add_job(db, user_id))
        waiting = parse_task(db, user_id, add_job(db, user_id), attempt_count=1,
                             next_attempt_at=utcnow() + timedelta(hours=1))
        db.commit()
        waiting_id, first_id = waiting.id, first.id
    out = signed_in(user_id).post(f"/api/workers/parsing/tasks/{waiting_id}/run-now").json()
    assert out["run_now"] is True and out["next_attempt_at"] is None
    with SessionLocal() as db:
        claims = job_description.claim(db, worker_id="w", limit=500)
        mine = [c for c in claims if c.task_id in (waiting_id, first_id)]
        assert mine[0].task_id == waiting_id and mine[0].run_now is True
        assert next(c for c in mine if c.task_id == first_id).run_now is False
        assert db.get(JobDescriptionParseTask, waiting_id).run_now_at is None  # consumed by the claim


def test_run_now_on_a_recovery_task_is_carried_by_its_claim(user_id, signed_in):
    with SessionLocal() as db:
        task = JobDescriptionFetchTask(user_id=user_id, job_id=add_job(db, user_id).id, status="PENDING",
                                       attempt_count=1, next_attempt_at=utcnow() + timedelta(hours=1))
        db.add(task)
        db.commit()
        task_id = task.id
    signed_in(user_id).post(f"/api/workers/recovery/tasks/{task_id}/run-now").raise_for_status()
    with SessionLocal() as db:
        claim = next(c for c in jd_recovery.claim(db, worker_id="w", limit=500) if c.task_id == task_id)
    assert claim.run_now is True


def test_run_now_requeues_a_failed_task_and_refuses_running_or_foreign_ones(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        failed = parse_task(db, user_id, add_job(db, user_id), status="FAILED", attempt_count=3,
                            completed_at=utcnow(), last_error="boom")
        running = parse_task(db, user_id, add_job(db, user_id), status="PROCESSING", lock_token="t")
        theirs = parse_task(db, other, add_job(db, other))
        ranking_job = add_job(db, user_id, state="RANK_PENDING")
        rank = JobRankingTask(user_id=user_id, job_id=ranking_job.id, status="PENDING")
        db.add(rank)
        db.commit()
        ids = failed.id, running.id, theirs.id, rank.id
    client = signed_in(user_id)
    out = client.post(f"/api/workers/parsing/tasks/{ids[0]}/run-now").json()
    assert out["task_id"] != ids[0] and out["status"] == "QUEUED" and out["run_now"] is True
    assert client.post(f"/api/workers/parsing/tasks/{ids[1]}/run-now").status_code == 409
    assert client.post(f"/api/workers/parsing/tasks/{ids[2]}/run-now").status_code == 404
    client.post(f"/api/workers/ranking/tasks/{ids[3]}/run-now").raise_for_status()
    with SessionLocal() as db:
        task = db.get(JobRankingTask, ids[3])
        assert task.force is True and task.run_now_at is not None  # the cadence never holds it back


def test_a_bypassed_call_skips_the_cooldown_but_others_still_wait(user_id):
    with SessionLocal() as db:
        db.add(ProviderCooldown(user_id=user_id, provider="OPENROUTER", until=utcnow() + timedelta(minutes=5),
                                consecutive=1, calls_skipped=0))
        db.commit()
    call = external_api.ExternalCall(provider="OPENROUTER", service="llm", operation="chat", user_id=user_id)
    with pytest.raises(external_api.RateLimitedCooldown):
        external_api.hold_off(call)
    with backoff.bypass():
        external_api.hold_off(call)  # no exception: this task was asked to run now
    with backoff.bypass(False):
        with pytest.raises(external_api.RateLimitedCooldown):
            external_api.hold_off(call)


def test_reset_cooldowns_endpoint(user_id, signed_in):
    with SessionLocal() as db:
        for provider in ("OPENROUTER", "APIFY"):
            db.add(ProviderCooldown(user_id=user_id, provider=provider, until=utcnow() + timedelta(minutes=5),
                                    consecutive=1, calls_skipped=0))
        db.commit()
    client = signed_in(user_id)
    assert client.post("/api/workers/cooldowns/reset?provider=apify").json() == {"cleared": ["APIFY"]}
    assert [c["provider"] for c in client.get("/api/workers").json()["cooldowns"]] == ["OPENROUTER"]


def test_kinds_registry_is_complete():
    assert set(workers.KINDS) == {"parsing", "recovery", "ranking", "insights", "tailoring", "reviewing"}
    for spec in workers.KINDS.values():
        assert hasattr(spec.model, "run_now_at") and spec.max_attempts() >= 1


# ───────────── reviewing: the review step of a tailoring task ─────────────

def test_reviewing_shows_the_tailor_tasks_at_the_review_step(user_id, signed_in):
    with SessionLocal() as db:
        tailoring, reviewing, review_only, reviewed, failed_early = (add_job(db, user_id) for _ in range(5))
        lease = dict(locked_by="w", lock_expires_at=utcnow() + timedelta(minutes=5), started_at=utcnow())
        db.add(TailorTask(user_id=user_id, job_id=tailoring.id, status="running", stage="tailoring", attempts=1, **lease))
        review = TailorTask(user_id=user_id, job_id=reviewing.id, status="running", stage="reviewing", attempts=1, **lease)
        db.add(review)
        db.add(TailorTask(user_id=user_id, job_id=review_only.id, mode="skip", status="queued", stage="queued", attempts=0))
        db.add(TailorTask(user_id=user_id, job_id=reviewed.id, status="succeeded", stage="reviewing", attempts=1,
                          finished_at=utcnow(), result={"reviewer_score": 82}))
        early = TailorTask(user_id=user_id, job_id=failed_early.id, status="failed", stage="tailoring", attempts=1,
                           finished_at=utcnow(), error="boom")
        db.add(early)
        db.commit()
        review_id, early_id, job_ids = review.id, early.id, {reviewing.id, review_only.id, reviewed.id}
    client = signed_in(user_id)
    summary = client.get("/api/workers").json()
    kinds = {w["kind"]: w for w in summary["workers"]}
    assert kinds["reviewing"]["counts"] == {"running": 1, "queued": 1, "retrying": 0, "active": 2}
    assert kinds["reviewing"]["view_of"] == "tailoring"
    # The review step is part of a tailoring task: counted once in the totals.
    assert kinds["tailoring"]["counts"]["active"] == 3 and summary["active"] == 3 and summary["running"] == 2
    rows = client.get("/api/workers/reviewing").json()["rows"]
    assert {r["job_id"] for r in rows} == job_ids
    done = next(r for r in rows if r["status"] == "SUCCEEDED")
    assert done["reviewer_score"] == 82 and done["stage"] == "done"
    # A task outside the review step is not Reviewing's to touch.
    assert client.post(f"/api/workers/reviewing/tasks/{early_id}/run-now").status_code == 404
    out = client.post(f"/api/workers/reviewing/tasks/{review_id}/kill").json()
    assert (out["stopped"], out["running"]) == (1, 1)
    with SessionLocal() as db:
        task = db.get(TailorTask, review_id)
        assert (task.status, task.stage, task.locked_by) == ("cancelled", "reviewing", None)
    stopped = next(r for r in client.get("/api/workers/reviewing").json()["rows"] if r["task_id"] == review_id)
    assert stopped["status"] == "STOPPED"


def test_kill_all_workers_does_not_stop_the_review_step_twice(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="running", stage="reviewing", attempts=1,
                          locked_by="w", lock_expires_at=utcnow() + timedelta(minutes=5), started_at=utcnow()))
        db.commit()
    with SessionLocal() as db:
        out = workers.kill_all(db, user_id)
    assert "reviewing" not in out["workers"] and out["stopped"] == 1


# ───────────── filters ─────────────

def test_tasks_filter_by_search_and_status_with_counts_per_status(user_id, signed_in):
    with SessionLocal() as db:
        canva = add_job(db, user_id, title="Staff SWE", company="Canva")
        stripe = add_job(db, user_id, title="Backend Lead", company="Stripe")
        grab = add_job(db, user_id, title="AI Engineer", company="Grab")
        parse_task(db, user_id, canva, status="PROCESSING", lock_token="t", started_at=utcnow())
        parse_task(db, user_id, stripe, attempt_count=1, next_attempt_at=utcnow() + timedelta(minutes=5))
        parse_task(db, user_id, grab, status="FAILED", completed_at=utcnow(), last_error="boom")
        db.commit()
        grab_id = grab.id
    client = signed_in(user_id)
    every = client.get("/api/workers/parsing").json()
    assert every["status_counts"] == {"active": 2, "running": 1, "queued": 0, "retrying": 1, "failed": 1, "done": 0,
                                      "all": 3}
    assert [r["company"] for r in client.get("/api/workers/parsing?status=failed").json()["rows"]] == ["Grab"]
    assert [r["company"] for r in client.get("/api/workers/parsing?status=active").json()["rows"]] == ["Canva", "Stripe"]
    named = client.get("/api/workers/parsing?q=stri").json()
    assert [r["company"] for r in named["rows"]] == ["Stripe"] and named["status_counts"]["all"] == 1
    assert [r["company"] for r in client.get("/api/workers/parsing?q=backend lead").json()["rows"]] == ["Stripe"]
    assert [r["job_id"] for r in client.get(f"/api/workers/parsing?q=%23{grab_id}").json()["rows"]] == [grab_id]
    assert client.get("/api/workers/parsing?q=hooli").json()["rows"] == []
    assert client.get("/api/workers/parsing?status=all").json()["total"] == 3
    assert client.get("/api/workers/parsing?status=sleeping").status_code == 422
    # Filters narrow the task list, never the metrics.
    assert every["metrics"] == client.get("/api/workers/parsing?status=failed").json()["metrics"]


def test_activity_counts_the_last_day_hour_by_hour(user_id, signed_in):
    now = utcnow()
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        a, b, c, d = (add_job(db, user_id) for _ in range(4))
        parse_task(db, user_id, a, status="COMPLETED", created_at=now - timedelta(hours=3),
                   started_at=now - timedelta(hours=3) + timedelta(seconds=30),
                   completed_at=now - timedelta(hours=3) + timedelta(seconds=90))
        parse_task(db, user_id, b, status="FAILED", created_at=now - timedelta(minutes=20),
                   started_at=now - timedelta(minutes=19), completed_at=now - timedelta(minutes=18))
        parse_task(db, user_id, c, status="COMPLETED", created_at=now - timedelta(days=3),
                   completed_at=now - timedelta(days=2))  # outside the day
        parse_task(db, user_id, d)  # queued now
        parse_task(db, other, add_job(db, other), status="FAILED", completed_at=now)  # another account
        db.commit()
    out = signed_in(user_id).get("/api/workers/parsing/activity").json()
    assert len(out["hours"]) == 24
    assert (out["done"], out["failed"], out["queued"]) == (1, 1, 3)
    assert out["success_rate"] == 0.5
    assert out["median_run_seconds"] == 60.0 and out["median_wait_seconds"] == 45.0
    assert sum(h["failed"] for h in out["hours"][-2:]) == 1 and out["hours"][-1]["queued"] >= 1
    assert signed_in(user_id).get("/api/workers/nope/activity").status_code == 404


def test_a_failed_task_run_again_cannot_be_run_now_twice(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        failed = parse_task(db, user_id, job, status="FAILED", attempt_count=3, completed_at=utcnow(), last_error="502")
        db.commit()
        failed_id = failed.id
    client = signed_in(user_id)
    assert next(r for r in client.get("/api/workers/parsing").json()["rows"] if r["task_id"] == failed_id)["can_run_now"]
    fresh = client.post(f"/api/workers/parsing/tasks/{failed_id}/run-now").json()
    assert fresh["task_id"] != failed_id and fresh["run_now"] is True
    old = next(r for r in client.get("/api/workers/parsing").json()["rows"] if r["task_id"] == failed_id)
    assert old["can_run_now"] is False
    assert old["retried_by"] == {"task_id": fresh["task_id"], "status": "QUEUED"}
    again = client.post(f"/api/workers/parsing/tasks/{failed_id}/run-now")
    assert again.status_code == 409 and f"#{fresh['task_id']}" in again.json()["detail"]
    with SessionLocal() as db:  # still exactly one new task for the job
        assert len([t for t in db.query(JobDescriptionParseTask).filter_by(job_id=fresh["job_id"]).all()
                    if t.status == "PENDING"]) == 1


# ───────────── bulk retry ─────────────

def test_bulk_retry_queues_each_failed_task_once_and_says_why_others_were_skipped(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        jobs = [add_job(db, user_id) for _ in range(4)]
        a = parse_task(db, user_id, jobs[0], status="FAILED", attempt_count=3, completed_at=utcnow(), last_error="502")
        b = parse_task(db, user_id, jobs[1], status="FAILED", attempt_count=3, completed_at=utcnow(), last_error="502")
        old = parse_task(db, user_id, jobs[2], status="FAILED", attempt_count=3, completed_at=utcnow())
        parse_task(db, user_id, jobs[2])  # old was already run again
        running = parse_task(db, user_id, jobs[3], status="PROCESSING", lock_token="t", started_at=utcnow())
        theirs = parse_task(db, other, add_job(db, other), status="FAILED", completed_at=utcnow())
        db.commit()
        ids = [a.id, b.id, old.id, running.id, theirs.id]
    client = signed_in(user_id)
    out = client.post("/api/workers/parsing/retry", json={"task_ids": ids + [a.id]}).json()
    assert [r["task_id"] for r in out["retried"]] == [a.id, b.id]
    reasons = {s["task_id"]: s["reason"] for s in out["skipped"]}
    assert reasons[old.id].startswith("Already run again as task #")
    assert reasons[running.id] == "Not failed (running)" and reasons[theirs.id] == "Not found"
    with SessionLocal() as db:
        for r in out["retried"]:
            fresh = db.get(JobDescriptionParseTask, r["new_task_id"])
            assert (fresh.status, fresh.job_id, fresh.run_now_at) == ("PENDING", r["job_id"], None)  # normal order
    again = client.post("/api/workers/parsing/retry", json={"task_ids": [a.id]}).json()
    assert again["retried"] == [] and again["skipped"][0]["reason"].startswith("Already run again")
    assert client.post("/api/workers/parsing/retry", json={"task_ids": []}).status_code == 422
    assert client.post("/api/workers/nope/retry", json={"task_ids": [1]}).status_code == 404


def test_bulk_retry_requeues_failed_tailoring_with_its_mode(user_id, signed_in):
    with SessionLocal() as db:
        db.execute(delete(TailorTask))
        job = add_job(db, user_id, status="shortlisted")
        task = TailorTask(user_id=user_id, job_id=job.id, status="failed", stage="done", attempts=2, mode="tailor",
                          error="Cannot connect to host ollama.com:443", finished_at=utcnow())
        db.add(task)
        db.commit()
        task_id = task.id
    out = signed_in(user_id).post("/api/workers/tailoring/retry", json={"task_ids": [task_id]}).json()
    assert len(out["retried"]) == 1 and out["skipped"] == []
    with SessionLocal() as db:
        fresh = db.get(TailorTask, out["retried"][0]["new_task_id"])
        assert (fresh.status, fresh.mode, fresh.run_now_at) == ("queued", "tailor", None)
        db.execute(delete(TailorTask))
        db.commit()


# ───────────── clear finished ─────────────

def test_clear_finished_hides_the_list_only_and_can_be_undone(user_id, signed_in):
    with SessionLocal() as db:
        done = parse_task(db, user_id, add_job(db, user_id), status="COMPLETED", completed_at=utcnow() - timedelta(minutes=5))
        failed = parse_task(db, user_id, add_job(db, user_id), status="FAILED", completed_at=utcnow() - timedelta(minutes=3))
        waiting = parse_task(db, user_id, add_job(db, user_id))
        db.commit()
        ids = done.id, failed.id, waiting.id
    client = signed_in(user_id)
    before = client.get("/api/workers/parsing").json()
    assert {r["task_id"] for r in before["rows"]} >= set(ids) and before["cleared_at"] is None
    out = client.post("/api/workers/parsing/clear").json()
    assert out["cleared_at"]
    after = client.get("/api/workers/parsing").json()
    shown = {r["task_id"] for r in after["rows"]}
    assert ids[2] in shown and ids[0] not in shown and ids[1] not in shown  # queued work never hides
    assert after["cleared_hidden"] >= 2 and after["status_counts"]["failed"] == 0
    assert next(w for w in client.get("/api/workers").json()["workers"] if w["kind"] == "parsing")["cleared_at"]
    assert client.get("/api/workers/parsing/activity").json()["done"] >= 1  # history and charts keep them
    with SessionLocal() as db:
        assert db.get(JobDescriptionParseTask, ids[0]) is not None  # nothing deleted
    client.delete("/api/workers/parsing/clear").raise_for_status()
    assert {r["task_id"] for r in client.get("/api/workers/parsing").json()["rows"]} >= set(ids)
    # Ranking shares the tracker's own clear.
    client.post("/api/workers/ranking/clear").raise_for_status()
    with SessionLocal() as db:
        assert ranker.finished_cleared_at(db, user_id) is not None
    assert client.post("/api/workers/nope/clear").status_code == 404
