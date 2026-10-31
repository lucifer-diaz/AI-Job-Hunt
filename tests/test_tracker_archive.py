"""Archiving old applications from the tracker (app.services.tracker.archive_older_than, POST /api/tracker/archive).

An application submitted more than the chosen number of days ago (0: any age) leaves the board (`archived_at`),
recorded on its own timeline; a live one also moves to Closed, a Rejected or Closed one keeps its stage. The scope
decides which stages are swept. A dry run only counts them; Unarchive, or moving the card, puts it back.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Application, ApplicationEvent, Job
from app.services.lifecycle import WorkflowState as S

client = TestClient(app)
CREATED: list[int] = []


@pytest.fixture(autouse=True)
def cleanup():
    """The test database is shared: remove this module's jobs (and their applications) so no other test sees them."""
    yield
    with SessionLocal() as db:
        db.info["user_id"] = _user()
        for job_id in CREATED:
            db.query(ApplicationEvent).filter(ApplicationEvent.application_id.in_(
                db.query(Application.id).filter(Application.job_id == job_id))).delete(synchronize_session=False)
            db.query(Application).filter(Application.job_id == job_id).delete(synchronize_session=False)
            db.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
        db.commit()
    CREATED.clear()


def _user() -> int:
    from tests.conftest import ensure_test_user

    return ensure_test_user().id


def submitted(days_ago: int, stage: str = "applied") -> int:
    """A submitted application `days_ago` days old in `stage`. Returns its id."""
    user = _user()
    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = Job(user_id=user, source="test", source_ref=uuid.uuid4().hex, title="Engineer",
                  company=f"Initech {uuid.uuid4().hex[:6]}", url=f"https://jobs.example.com/{uuid.uuid4().hex}",
                  description="Build services.", status="applied", state=S.APPLIED)
        db.add(job)
        db.flush()
        CREATED.append(job.id)
        application = Application(user_id=user, job_id=job.id, status="applied", stage=stage,
                                  applied_at=now - timedelta(days=days_ago))
        db.add(application)
        db.commit()
        return application.id


def _stage(app_id: int) -> str:
    with SessionLocal() as db:
        return db.get(Application, app_id).stage


def _archived(app_id: int) -> bool:
    with SessionLocal() as db:
        return db.get(Application, app_id).archived_at is not None


def test_a_dry_run_counts_without_moving_anything():
    old, recent = submitted(40), submitted(10)
    preview = client.post("/api/tracker/archive", json={"days": 30, "dry_run": True})
    assert preview.status_code == 200
    body = preview.json()
    assert body["count"] >= 1 and body["scope"] == "no_reply"
    assert {s["stage"] for s in body["by_stage"]} <= {"applied", "acknowledged"}
    assert _stage(old) == "applied" and _stage(recent) == "applied"


def test_archiving_moves_only_old_applications_without_a_reply():
    old, old_ack, recent, interviewing = submitted(46), submitted(46, "acknowledged"), submitted(14), \
        submitted(46, "interviewing")
    out = client.post("/api/tracker/archive", json={"days": 45})
    assert out.status_code == 200
    assert out.json()["count"] >= 2 and "tracker" in out.json()
    assert (_stage(old), _stage(old_ack), _stage(recent), _stage(interviewing)) == \
        ("closed", "closed", "applied", "interviewing")
    with SessionLocal() as db:
        event = db.query(ApplicationEvent).filter(ApplicationEvent.application_id == old,
                                                  ApplicationEvent.kind == "stage").one()
        assert event.title == "Archived: submitted more than 45 days ago" and event.detail == "closed"
        assert event.meta == {"archived_after_days": 45}
    assert _archived(old) and not _archived(recent) and not _archived(interviewing)


def test_every_live_stage_includes_interviews_but_never_rejected_ones():
    interviewing, rejected = submitted(20, "interviewing"), submitted(20, "rejected")
    assert client.post("/api/tracker/archive", json={"days": 15, "scope": "live"}).status_code == 200
    assert _stage(interviewing) == "closed" and _stage(rejected) == "rejected"


def test_rejected_and_closed_keep_their_stage_and_leave_the_board():
    rejected, closed, live = submitted(3, "rejected"), submitted(3, "closed"), submitted(3)
    out = client.post("/api/tracker/archive", json={"days": 0, "scope": "finished"})
    assert out.status_code == 200
    assert {s["stage"] for s in out.json()["by_stage"]} <= {"rejected", "closed"}
    assert (_stage(rejected), _stage(closed), _stage(live)) == ("rejected", "closed", "applied")
    assert (_archived(rejected), _archived(closed), _archived(live)) == (True, True, False)
    board = {a["id"]: a for a in out.json()["tracker"]["applications"]}
    assert board[rejected]["archived"] is True and board[live]["archived"] is False
    with SessionLocal() as db:
        event = db.query(ApplicationEvent).filter(ApplicationEvent.application_id == rejected).one()
        assert (event.kind, event.title, event.detail) == ("archive", "Archived", "rejected")
    # Archived ones are never counted again.
    again = client.post("/api/tracker/archive", json={"days": 0, "scope": "finished", "dry_run": True}).json()
    assert again["count"] == 0


def test_unarchive_and_moving_a_card_put_it_back_on_the_board():
    first, second = submitted(50, "rejected"), submitted(50)
    client.post("/api/tracker/archive", json={"days": 40, "scope": "all"})
    assert _archived(first) and _archived(second) and _stage(second) == "closed"
    back = client.post(f"/api/applications/{first}/unarchive")
    assert back.status_code == 200 and back.json()["archived"] is False and back.json()["stage"] == "rejected"
    assert client.post(f"/api/applications/{second}/stage", json={"stage": "applied"}).json()["archived"] is False
    assert not _archived(first) and not _archived(second)


def test_a_period_must_be_zero_or_more_days():
    assert client.post("/api/tracker/archive", json={"days": -1}).status_code == 422
    assert client.post("/api/tracker/archive", json={"days": 30, "scope": "everything"}).status_code == 422
