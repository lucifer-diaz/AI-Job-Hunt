"""How much work runs at once for one account, and what the ranking tracker says about the queue.

The limits are the account's own (Settings → Workflow & cadence), bounded by the server's ceilings, and they
are enforced where work is claimed — so they hold across restarts and across several workers.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import AppSetting, Job, JobRankingTask, TailorTask
from app.services import ranker, tailor_tasks, user_settings
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from tests.conftest import ensure_test_user

client = TestClient(app)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    with SessionLocal() as db:
        db.execute(delete(JobRankingTask).where(JobRankingTask.user_id == owner))
        db.execute(delete(TailorTask).where(TailorTask.user_id == owner))
        db.execute(delete(Job).where(Job.user_id == owner, Job.source == "limits"))
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == owner))
        if row is not None:
            row.preferences = {k: v for k, v in (row.preferences or {}).items() if k != "processing"}
        db.commit()
    yield


def set_limits(**limits):
    return client.patch("/api/settings", json=limits).json()


def queue_jobs(owner, count, *, status="PENDING", started=None, completed=None, minutes_ago=30):
    with SessionLocal() as db:
        ids = []
        for i in range(count):
            job = Job(user_id=owner, source="limits", source_ref=uuid.uuid4().hex, title=f"Associate PM {i}",
                      company="Acme", state=S.RANK_PENDING if status != "COMPLETED" else S.RANKED)
            db.add(job)
            db.flush()
            db.add(JobRankingTask(user_id=owner, job_id=job.id, status=status, reason="new",
                                  attempt_count=1 if status != "PENDING" else 0, started_at=started,
                                  completed_at=completed,
                                  created_at=utcnow() - timedelta(minutes=minutes_ago - i)))
            ids.append(job.id)
        db.commit()
        return ids


# ───────────── the setting ─────────────

def test_the_limits_default_to_the_server_ceiling_and_can_be_lowered(owner):
    settings = get_settings()
    out = client.get("/api/settings").json()
    assert out["processing_maximums"] == {"ranking_concurrency": settings.ranking_concurrency,
                                          "tailoring_concurrency": settings.tailor_concurrency}
    assert out["ranking_concurrency"] == settings.ranking_concurrency
    assert out["tailoring_concurrency"] == settings.tailor_concurrency

    saved = set_limits(ranking_concurrency=2, tailoring_concurrency=1)
    assert saved["ranking_concurrency"] == 2 and saved["tailoring_concurrency"] == 1
    assert client.get("/api/settings").json()["ranking_concurrency"] == 2


def test_an_account_cannot_ask_for_more_than_the_server_allows(owner):
    ceiling = get_settings().ranking_concurrency
    assert set_limits(ranking_concurrency=ceiling + 3)["ranking_concurrency"] == ceiling
    assert client.patch("/api/settings", json={"ranking_concurrency": 0}).status_code == 422
    assert client.patch("/api/settings", json={"tailoring_concurrency": 999}).status_code == 422


def test_saving_another_setting_leaves_the_limits_alone(owner):
    set_limits(ranking_concurrency=2, tailoring_concurrency=1)
    out = client.patch("/api/settings", json={"min_review_score": 81}).json()
    assert out["min_review_score"] == 81
    assert (out["ranking_concurrency"], out["tailoring_concurrency"]) == (2, 1)


def test_the_services_read_the_saved_limits(owner):
    set_limits(ranking_concurrency=3, tailoring_concurrency=1)
    with SessionLocal() as db:
        db.info["user_id"] = owner
        assert user_settings.processing_limits(db, owner) == {"ranking_concurrency": 3, "tailoring_concurrency": 1,
                                                              "ranking_interval_minutes": 0}
        assert user_settings.limits_for(db, [owner], "ranking_concurrency") == {owner: 3}
        # An account that has never saved a limit gets the server ceiling, not zero.
        assert user_settings.limits_for(db, [-1], "tailoring_concurrency") == {-1: get_settings().tailor_concurrency}


# ───────────── enforcement ─────────────

def test_ranking_claims_no_more_than_the_account_allows(owner, monkeypatch):
    """The claim itself is PostgreSQL-only, so the quota it uses is what is checked here."""
    set_limits(ranking_concurrency=2)
    queue_jobs(owner, 3, status="PROCESSING", started=utcnow())
    with SessionLocal() as db:
        quota = ranker._account_quota(db, {owner}, utcnow())
    assert quota[owner] == 0, "three already running against a limit of two leaves no room"

    with SessionLocal() as db:
        db.execute(delete(JobRankingTask).where(JobRankingTask.user_id == owner))
        db.commit()
    queue_jobs(owner, 1, status="PROCESSING", started=utcnow())
    with SessionLocal() as db:
        assert ranker._account_quota(db, {owner}, utcnow())[owner] == 1, "one running leaves room for one more"


def test_an_expired_lease_does_not_hold_a_ranking_slot(owner):
    set_limits(ranking_concurrency=1)
    ids = queue_jobs(owner, 1, status="PROCESSING", started=utcnow() - timedelta(hours=2))
    with SessionLocal() as db:
        task = db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == ids[0]))
        task.lock_expires_at = utcnow() - timedelta(minutes=5)
        db.commit()
        assert ranker._account_quota(db, {owner}, utcnow())[owner] == 1, "a crashed worker must not block the queue"


def test_tailoring_claims_no_more_than_the_account_allows(owner):
    set_limits(tailoring_concurrency=1)
    job_id = queue_jobs(owner, 1)[0]
    with SessionLocal() as db:
        db.add(TailorTask(user_id=owner, job_id=job_id, status="running", stage="tailoring"))
        db.add(TailorTask(user_id=owner, job_id=job_id, status="queued", stage="queued"))
        db.commit()
        assert tailor_tasks._account_quota(db, {owner}) == {owner: 0}
        assert tailor_tasks.claim_next(SessionLocal, "worker-1", 900) is None, "the queued packet waits its turn"

    with SessionLocal() as db:
        db.execute(delete(TailorTask).where(TailorTask.user_id == owner, TailorTask.status == "running"))
        db.commit()
    assert tailor_tasks.claim_next(SessionLocal, "worker-1", 900) is not None, "a freed slot lets the next one in"


# ───────────── how often ranking runs ─────────────

@pytest.fixture
def ready_account(monkeypatch):
    """An account the ranker would otherwise skip for missing a profile or resume: this is about the cadence."""
    monkeypatch.setattr(ranker, "readiness", lambda db, user_id: None)
    monkeypatch.setattr(ranker, "auto_rank_on", lambda db, user_id: True)


def test_an_account_can_ask_to_be_ranked_less_often(owner):
    """The server looks every RANKING_INTERVAL_MINUTES; an account may ask to be looked at less often."""
    assert client.get("/api/settings").json()["ranking_interval_minutes"] == 0, "every server pass by default"
    assert client.get("/api/settings").json()["ranking_interval_server_minutes"] == get_settings().ranking_interval_minutes

    assert set_limits(ranking_interval_minutes=30)["ranking_interval_minutes"] == 30
    assert client.patch("/api/settings", json={"ranking_interval_minutes": 5000}).status_code == 422
    assert client.patch("/api/settings", json={"ranking_interval_minutes": -1}).status_code == 422
    with SessionLocal() as db:
        assert user_settings.ranking_interval_minutes(db, owner) == 30


def test_a_cadence_holds_an_account_back_until_it_is_due(owner, ready_account):
    set_limits(ranking_interval_minutes=30)
    queue_jobs(owner, 1)
    now = utcnow()
    queue_jobs(owner, 1, status="COMPLETED", started=now - timedelta(minutes=5),
               completed=now - timedelta(minutes=4))
    with SessionLocal() as db:
        assert ranker.next_run_at(db, owner, now) is not None, "five minutes into a thirty minute cadence"
        ready, _, waiting = ranker.accounts(db, now)
        assert owner not in ready
        assert "Waiting for this account's next ranking run" in waiting

    with SessionLocal() as db:  # once the wait has passed
        db.query(JobRankingTask).filter(JobRankingTask.status == "COMPLETED").update(
            {"started_at": now - timedelta(minutes=45)})
        db.commit()
        assert ranker.next_run_at(db, owner, now) is None
        ready, _, _ = ranker.accounts(db, now)
        assert owner in ready


def test_no_cadence_means_every_server_pass(owner, ready_account):
    queue_jobs(owner, 1)
    queue_jobs(owner, 1, status="COMPLETED", started=utcnow() - timedelta(seconds=10),
               completed=utcnow() - timedelta(seconds=5))
    with SessionLocal() as db:
        assert ranker.next_run_at(db, owner, utcnow()) is None
        assert owner in ranker.accounts(db, utcnow())[0]


def test_an_account_that_has_never_ranked_is_never_held_back(owner):
    set_limits(ranking_interval_minutes=120)
    queue_jobs(owner, 1)
    with SessionLocal() as db:
        assert ranker.next_run_at(db, owner, utcnow()) is None, "there is nothing to wait for yet"


def test_the_tracker_reports_the_cadence(owner):
    set_limits(ranking_interval_minutes=15)
    now = utcnow()
    queue_jobs(owner, 1)
    queue_jobs(owner, 1, status="COMPLETED", started=now - timedelta(minutes=2), completed=now - timedelta(minutes=1))
    out = client.get("/api/ranking/tracker").json()
    assert out["interval_minutes"] == 15 and out["next_run_at"] is not None


# ───────────── what the tracker reports ─────────────

def test_every_waiting_job_knows_its_place_and_how_far_along_it_is(owner):
    set_limits(ranking_concurrency=2)
    queue_jobs(owner, 1, status="PROCESSING", started=utcnow() - timedelta(seconds=30))
    queue_jobs(owner, 5)
    # Two finished rankings give the estimate something measured to work from.
    queue_jobs(owner, 2, status="COMPLETED", started=utcnow() - timedelta(minutes=10),
               completed=utcnow() - timedelta(minutes=9))

    with SessionLocal() as db:
        out = ranker.tracker(db, owner, utcnow())
    assert out["concurrency"] == 2 and out["typical_seconds"] == 60 and out["in_flight"] == 6
    active = out["active"]
    assert [t["queue_position"] for t in active] == [1, 2, 3, 4, 5, 6]
    # One of the two slots is busy, so only the next job in line can start now; the rest wait their turn.
    assert [t["stage"] for t in active] == ["ranking", "queued", "waiting", "waiting", "waiting", "waiting"]
    assert active[0]["percent"] > 60 and active[0]["elapsed_seconds"] == 30
    assert active[0]["eta_seconds"] == 30, "half of a typical ranking is left"
    assert active[1]["eta_seconds"] == 0, "the next one starts as soon as the worker runs"
    assert active[3]["eta_seconds"] == 60 and active[5]["eta_seconds"] == 120, "one wave per two jobs"
    assert all(t["stage_label"] for t in active)


def test_a_running_ranking_never_shows_as_finished(owner):
    queue_jobs(owner, 1, status="PROCESSING", started=utcnow() - timedelta(hours=3))
    queue_jobs(owner, 2, status="COMPLETED", started=utcnow() - timedelta(minutes=10),
               completed=utcnow() - timedelta(minutes=9))
    with SessionLocal() as db:
        running = ranker.tracker(db, owner, utcnow())["active"][0]
    assert running["percent"] == ranker.RANKING_CEILING < 100


def test_no_estimate_is_invented_before_anything_has_been_measured(owner):
    queue_jobs(owner, 3)
    with SessionLocal() as db:
        out = ranker.tracker(db, owner, utcnow())
    assert out["typical_seconds"] is None
    assert all(t["eta_seconds"] is None for t in out["active"]), "unknown timing stays unknown"
    assert all(t["percent"] is not None for t in out["active"]), "the stage is still known"


def test_a_retrying_job_counts_down_to_its_next_attempt(owner):
    ids = queue_jobs(owner, 1)
    with SessionLocal() as db:
        task = db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == ids[0]))
        task.attempt_count, task.next_attempt_at = 1, utcnow() + timedelta(minutes=5)
        task.last_error = "The provider was busy"
        db.commit()
        entry = ranker.tracker(db, owner, utcnow())["active"][0]
    assert entry["stage"] == "retry" and entry["stage_label"] == "Waiting to retry"
    assert 290 <= entry["eta_seconds"] <= 300


def test_the_tracker_endpoint_carries_the_progress_to_the_screen(owner):
    queue_jobs(owner, 2)
    out = client.get("/api/ranking/tracker").json()
    assert out["concurrency"] >= 1 and "in_flight" in out
    assert {"queue_position", "stage", "percent", "eta_seconds"} <= set(out["active"][0])
