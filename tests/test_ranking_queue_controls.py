"""The ranking tracker can be filtered, and a waiting job can be taken out of the ranking queue (and put back)."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Job, JobRankingTask, WorkflowTransition
from app.services import ranker
from app.services.lifecycle import WorkflowState as S
from tests.conftest import ensure_test_user

client = TestClient(app)
POSTING = "We are hiring a lead engineer to own our platform roadmap. " * 8


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    def wipe():
        with SessionLocal() as db:
            ids = select(Job.id).where(Job.user_id == owner, Job.source == "queue-controls")
            db.execute(delete(JobRankingTask).where(JobRankingTask.job_id.in_(ids)))
            db.execute(delete(WorkflowTransition).where(WorkflowTransition.job_id.in_(ids)))
            db.execute(delete(Job).where(Job.id.in_(ids)))
            db.commit()
    wipe()
    yield
    wipe()


def queued(owner, title, location, *, status="PENDING", minutes_ago=30):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="queue-controls", source_ref=uuid.uuid4().hex, title=title, company="Acme",
                  location=location, description=POSTING,
                  state=S.RANKING if status == "PROCESSING" else S.RANK_PENDING)
        db.add(job)
        db.flush()
        db.add(JobRankingTask(user_id=owner, job_id=job.id, status=status, reason="new", attempt_count=0,
                              created_at=utcnow() - timedelta(minutes=minutes_ago)))
        db.commit()
        return job.id


def tracker(**params):
    return client.get("/api/ranking/tracker", params=params).json()


# ───────────── filtering ─────────────

def test_the_tracker_filters_by_location_and_search_but_keeps_real_queue_places(owner):
    tag = uuid.uuid4().hex[:8]  # other tests share this account, so names and cities are this test's own
    city, other = f"Bengaluru{tag}", f"Singapore{tag}"
    blr = queued(owner, f"Lead Engineer {tag}", f"{city}, Karnataka, India", minutes_ago=30)
    sgp = queued(owner, f"Staff Engineer {tag}", other, minutes_ago=20)
    blr2 = queued(owner, f"Platform Lead {tag}", f"{city}, India", minutes_ago=10)

    everything = tracker()
    assert dict(everything["locations"])[city] == 2 and everything["filtered"] is False
    places = {t["job_id"]: t["queue_position"] for t in everything["active"]}

    only_blr = tracker(location=city)
    assert [t["job_id"] for t in only_blr["active"]] == [blr, blr2] and only_blr["filtered"] is True
    assert [t["queue_position"] for t in only_blr["active"]] == [places[blr], places[blr2]], \
        "a filtered job still shows its place in the whole queue"
    assert only_blr["in_flight"] == everything["in_flight"], "filters narrow the lists, never the counts"
    assert [t["job_id"] for t in tracker(q=f"staff engineer {tag}")["active"]] == [sgp]
    assert tracker(q=f"staff engineer {tag}", location=city)["active"] == []


# ───────────── removing a job from the queue ─────────────

def test_a_waiting_job_can_be_removed_and_is_not_queued_again(owner):
    job_id = queued(owner, "Lead Engineer", "Singapore")
    out = client.post("/api/ranking/remove", json={"ids": [job_id]}).json()
    assert out == {"removed": [job_id], "skipped": []}
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        task = db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == job_id))
        assert job.state is S.SKIPPED and task.status == "SKIPPED" and task.last_error == ranker.REMOVED_BY_USER
        # The sweep (PostgreSQL only) queues jobs in these states and nothing else, so it never puts this one back.
        assert job.state not in (*ranker.PRE_RANK_STATES, S.RANK_PENDING)
    row = next(t for t in tracker()["recent"] if t["job_id"] == job_id)
    assert row["removed"] is True, "the tracker offers to rank it again"


def test_a_job_being_ranked_right_now_cannot_be_removed(owner):
    job_id = queued(owner, "Lead Engineer", "Singapore", status="PROCESSING")
    out = client.post("/api/ranking/remove", json={"ids": [job_id]}).json()
    assert out["removed"] == [] and out["skipped"] == [{"id": job_id, "reason": "This job is being ranked right now"}]


def test_asking_to_rank_a_removed_job_puts_it_back_in_the_queue(owner):
    job_id = queued(owner, "Lead Engineer", "Singapore")
    client.post("/api/ranking/remove", json={"ids": [job_id]})
    response = client.post(f"/api/jobs/{job_id}/rank")
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        assert db.get(Job, job_id).state is S.RANK_PENDING
        tasks = db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == job_id)
                           .order_by(JobRankingTask.id)).all()
    assert [t.status for t in tasks] == ["SKIPPED", "PENDING"]
    assert next(t for t in tracker()["recent"] if t["job_id"] == job_id)["removed"] is False


def test_asking_to_rank_a_stopped_job_puts_it_back_in_the_queue(owner):
    """A ranking stopped from Running workers leaves the job SKIPPED; ranking it from its own screen works again."""
    job_id = queued(owner, "Lead Engineer", "Singapore", status="PROCESSING")
    with SessionLocal() as db:
        task_id = db.scalar(select(JobRankingTask.id).where(JobRankingTask.job_id == job_id))
    assert client.post(f"/api/workers/ranking/tasks/{task_id}/kill").status_code == 200
    with SessionLocal() as db:
        assert db.get(Job, job_id).state is S.SKIPPED
    response = client.post(f"/api/jobs/{job_id}/rank")
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        assert db.get(Job, job_id).state is S.RANK_PENDING
        tasks = db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == job_id)
                           .order_by(JobRankingTask.id)).all()
    assert [t.status for t in tasks] == ["SKIPPED", "PENDING"]


def test_only_a_skip_the_user_made_can_be_undone(owner):
    """A job skipped for any other reason (here: dismissed in triage) stays skipped when ranking is asked for."""
    job_id = queued(owner, "Lead Engineer", "Singapore")
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        from app.services.work_queue import transition
        transition(db, job, S.SKIPPED, actor="test", reason="Dismissed in triage")
        db.commit()
    assert client.post(f"/api/jobs/{job_id}/rank").status_code == 409


# ───────────── clearing the finished list ─────────────

def finished(owner, title, status="FAILED", minutes_ago=5):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="queue-controls", source_ref=uuid.uuid4().hex, title=title, company="Acme",
                  description=POSTING, state=S.RANK_PENDING)
        db.add(job)
        db.flush()
        db.add(JobRankingTask(user_id=owner, job_id=job.id, status=status, reason="new", attempt_count=3,
                              last_error="LLMRateLimitError: 429", created_at=utcnow() - timedelta(minutes=minutes_ago),
                              completed_at=utcnow() - timedelta(minutes=minutes_ago)))
        db.commit()
        return job.id


def test_clearing_the_finished_list_hides_it_without_deleting_anything(owner):
    client.delete("/api/ranking/finished/clear")  # start from an uncleared list
    old = finished(owner, f"Engineer {uuid.uuid4().hex[:6]}")
    assert old in {t["job_id"] for t in tracker()["recent"]}

    assert client.post("/api/ranking/finished/clear").json() == {"ok": True}
    after = tracker()
    assert old not in {t["job_id"] for t in after["recent"]} and after["finished_cleared_at"]
    assert after["counts"]["failed"] == 0, "the counts start at the clear too"
    with SessionLocal() as db:
        assert db.scalar(select(JobRankingTask).where(JobRankingTask.job_id == old)) is not None, "history is kept"

    new = finished(owner, f"Engineer {uuid.uuid4().hex[:6]}", minutes_ago=-1)  # finishes after the clear
    assert new in {t["job_id"] for t in tracker()["recent"]}

    client.delete("/api/ranking/finished/clear")
    shown = tracker()
    assert {old, new} <= {t["job_id"] for t in shown["recent"]} and shown["finished_cleared_at"] is None
