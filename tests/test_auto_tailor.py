"""Automatic tailoring: ranking queues the jobs it qualifies for tailoring when the account turned it on.

The ranking tests need PostgreSQL (TEST_DATABASE_URL); the settings API test also runs on SQLite.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Job, TailorTask
from app.services import auto_tailor, ranker
from app.services import scheduled_jobs as sj
from app.services.user_settings import auto_tailor_for, settings_for
from tests.test_ranker import FakeModel, add_jobs, add_owner, ctx, rank, utcnow


def turn_on(sessions, user_id, *, min_fit_score=None, since=None, execution_mode="backend"):
    with sessions() as db:
        db.info["user_id"] = user_id
        row = settings_for(db, user_id)
        row.preferences = {**(row.preferences or {}), "execution_mode": execution_mode,
                           "auto_tailor": {"enabled": True, "min_fit_score": min_fit_score,
                                           "enabled_at": (since or utcnow() - timedelta(seconds=5)).isoformat()}}
        db.commit()


def tailor_tasks_of(sessions, job_id):
    with sessions() as db:
        return db.scalars(select(TailorTask).where(TailorTask.job_id == job_id)).all()


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(ranker, "LLMClient", fake)
    return fake


@pytest.fixture
def owner(pg_sessions):
    return add_owner(pg_sessions)


def test_a_qualified_job_is_queued_for_tailoring_straight_after_ranking(pg_sessions, owner, model):
    turn_on(pg_sessions, owner)
    job_id, = add_jobs(pg_sessions, 1, owner)
    assert rank(pg_sessions)["qualified"] == 1
    task, = tailor_tasks_of(pg_sessions, job_id)
    assert (task.status, task.mode, task.user_id) == ("queued", "tailor", owner)


def test_nothing_is_queued_when_it_is_off_or_archived(pg_sessions, owner, model):
    qualified, archived = add_jobs(pg_sessions, 2, owner)
    model.answer(archived, {"overall_fit_score": 30})
    rank(pg_sessions)
    assert tailor_tasks_of(pg_sessions, qualified) == []  # off by default
    turn_on(pg_sessions, owner, since=utcnow() - timedelta(hours=1))
    with pg_sessions() as db:
        db.info["user_id"] = owner
        assert auto_tailor.sweep(db) == {"queued": 1, "skipped": 0}  # the qualified one, ranked after enabled_at
    assert tailor_tasks_of(pg_sessions, archived) == []


def test_only_jobs_at_the_automatic_fit_score_are_queued(pg_sessions, owner, model):
    turn_on(pg_sessions, owner, min_fit_score=80)
    high, low = add_jobs(pg_sessions, 2, owner)
    model.answer(high, {"overall_fit_score": 85})
    model.answer(low, {"overall_fit_score": 75})  # qualified (minimum 70) but below the automatic threshold
    assert rank(pg_sessions)["qualified"] == 2
    assert len(tailor_tasks_of(pg_sessions, high)) == 1
    assert tailor_tasks_of(pg_sessions, low) == []


def test_jobs_ranked_before_it_was_turned_on_are_not_backfilled(pg_sessions, owner, model):
    job_id, = add_jobs(pg_sessions, 1, owner)
    rank(pg_sessions)
    turn_on(pg_sessions, owner, since=utcnow() + timedelta(seconds=1))
    with pg_sessions() as db:
        db.info["user_id"] = owner
        assert auto_tailor.sweep(db) == {"queued": 0, "skipped": 0}
    assert tailor_tasks_of(pg_sessions, job_id) == []


def test_claude_desktop_mode_never_queues(pg_sessions, owner, model):
    turn_on(pg_sessions, owner, execution_mode="claude_desktop")
    job_id, = add_jobs(pg_sessions, 1, owner)
    rank(pg_sessions)
    assert tailor_tasks_of(pg_sessions, job_id) == []


def test_a_cancelled_task_is_never_queued_again(pg_sessions, owner, model):
    turn_on(pg_sessions, owner)
    job_id, = add_jobs(pg_sessions, 1, owner)
    rank(pg_sessions)
    with pg_sessions() as db:
        task = db.scalars(select(TailorTask).where(TailorTask.job_id == job_id)).one()
        task.status = "cancelled"
        db.commit()
    with pg_sessions() as db:
        db.info["user_id"] = owner
        assert auto_tailor.sweep(db) == {"queued": 0, "skipped": 0}


def test_a_failure_to_queue_never_changes_the_ranking(pg_sessions, owner, model, monkeypatch):
    turn_on(pg_sessions, owner)

    def broken(db, ids, *args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(auto_tailor.tailor_tasks, "enqueue", broken)
    job_id, = add_jobs(pg_sessions, 1, owner)
    assert rank(pg_sessions)["qualified"] == 1
    with pg_sessions() as db:
        assert db.get(Job, job_id).status == "shortlisted"
    monkeypatch.undo()
    # The scheduler's sweep catches what the hook missed.
    summary = sj.auto_tailor_job(ctx(pg_sessions))
    assert summary["queued"] == 1 and summary["accounts"] == 1
    assert len(tailor_tasks_of(pg_sessions, job_id)) == 1


def test_the_sweep_skips_when_no_account_has_it_on(pg_sessions, owner):
    assert "skipped" in sj.auto_tailor_job(ctx(pg_sessions))


def test_the_settings_api_records_when_it_was_turned_on():
    client = TestClient(app)
    client.patch("/api/settings", json={"auto_tailor": False})
    first = client.patch("/api/settings", json={"auto_tailor": True, "auto_tailor_min_fit_score": 80}).json()
    assert first["auto_tailor"]["enabled"] and first["auto_tailor"]["min_fit_score"] == 80
    assert first["auto_tailor"]["enabled_at"]
    again = client.patch("/api/settings", json={"auto_tailor_min_fit_score": 0}).json()["auto_tailor"]
    assert again["enabled_at"] == first["auto_tailor"]["enabled_at"]  # staying on keeps the boundary
    assert again["min_fit_score"] is None  # 0 = every qualified job
    off = client.patch("/api/settings", json={"auto_tailor": False}).json()["auto_tailor"]
    assert off == {"enabled": False, "min_fit_score": None, "enabled_at": None}
    assert client.patch("/api/settings", json={"auto_tailor_min_fit_score": 101}).status_code == 422


def test_auto_tailor_for_tolerates_bad_saved_values(monkeypatch):
    """A saved fit score that is not a number reads as "every qualified job"."""
    import app.services.user_settings as us

    row = type("Row", (), {"preferences": {"auto_tailor": {"enabled": True, "min_fit_score": "high",
                                                           "enabled_at": "2026-09-28T10:00:00"}}})()
    monkeypatch.setattr(us, "settings_for", lambda db, user_id=None: row)
    assert auto_tailor_for(None) == {"enabled": True, "min_fit_score": None, "enabled_at": "2026-09-28T10:00:00"}
