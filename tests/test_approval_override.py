"""Approving a packet the reviewer scored below the account's minimum.

The minimum is the account's own line (Settings → Workflow & cadence). It can be waived for one packet, on
purpose and on the record — but review itself cannot be skipped, and waiving one packet never moves the line.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Application, ApplicationEvent, AppSetting, Job, ResumeVariant
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from tests.conftest import ensure_test_user

client = TestClient(app)
MINIMUM = 55


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def owner():
    user_id = ensure_test_user().id
    with SessionLocal() as db:
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
        if row is None:
            row = AppSetting(user_id=user_id)
            db.add(row)
        row.min_review_score = MINIMUM
        db.commit()
    return user_id


@pytest.fixture(autouse=True)
def clean(owner):
    """Remove this file's jobs and everything hanging off them.

    SQLite does not enforce the ON DELETE CASCADE the schema declares, so deleting only the jobs would leave
    orphaned applications and events behind — and reused row ids would then attach them to the next test.
    """
    with SessionLocal() as db:
        job_ids = list(db.scalars(select(Job.id).where(Job.user_id == owner, Job.source == "override-test")).all())
        if job_ids:
            application_ids = list(db.scalars(select(Application.id)
                                              .where(Application.job_id.in_(job_ids))).all())
            if application_ids:
                db.execute(delete(ApplicationEvent).where(ApplicationEvent.application_id.in_(application_ids)))
                db.execute(delete(Application).where(Application.id.in_(application_ids)))
            db.execute(delete(ResumeVariant).where(ResumeVariant.job_id.in_(job_ids)))
            db.execute(delete(Job).where(Job.id.in_(job_ids)))
        db.commit()
    yield


def make_packet(owner, score):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="override-test", source_ref=uuid.uuid4().hex, title="Lead Software Engineer",
                  company="Logitech", status="tailored", state=S.READY_TO_APPLY)
        db.add(job)
        db.flush()
        db.add(ResumeVariant(job_id=job.id, user_id=owner, version=1, markdown="# Rohan", reviewer_score=score))
        db.commit()
        return job.id


def approve(job_id, **body):
    response = client.post(f"/api/jobs/{job_id}/approve", json=body)
    assert response.status_code in (200, 400), f"{response.status_code}: {response.text}"
    return response


def test_a_low_score_is_refused_but_says_it_can_be_overridden(owner):
    job_id = make_packet(owner, 45)
    response = approve(job_id)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["can_override"] is True
    assert detail["reviewer_score"] == 45 and detail["min_review_score"] == MINIMUM
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "tailored", "a refusal changes nothing"


def test_the_minimum_can_be_waived_for_one_packet(owner):
    job_id = make_packet(owner, 45)
    out = approve(job_id, override=True, note="I checked the claims myself").json()
    assert out["approval_override"] is True
    assert out["reviewer_score"] == 45 and out["min_review_score"] == MINIMUM

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        assert job.status == "approved" and application.status == "approved"
        assert application.approval_override is True
        assert application.approval_note == "I checked the claims myself"
        assert application.approved_resume_id is not None
        # The account's own line is untouched: this waived one packet, not the setting.
        assert db.scalar(select(AppSetting.min_review_score).where(AppSetting.user_id == owner)) == MINIMUM


def test_waiving_is_written_into_the_tracker_timeline(owner):
    job_id = make_packet(owner, 30)
    approve(job_id, override=True, note="Recruiter asked for it today")
    with SessionLocal() as db:
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        event = db.scalar(select(ApplicationEvent).where(ApplicationEvent.application_id == application.id,
                                                         ApplicationEvent.kind == "approval"))
        assert event.title == "Approved below the reviewer minimum"
        assert event.detail == "Recruiter asked for it today"


def test_waiving_without_a_note_still_records_the_score(owner):
    job_id = make_packet(owner, 40)
    approve(job_id, override=True)
    with SessionLocal() as db:
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        event = db.scalar(select(ApplicationEvent).where(ApplicationEvent.application_id == application.id,
                                                         ApplicationEvent.kind == "approval"))
        assert application.approval_override is True and application.approval_note is None
        assert "40" in event.detail


def test_a_score_at_or_above_the_minimum_needs_no_override(owner):
    for score in (MINIMUM, 90):
        out = approve(make_packet(owner, score)).json()
        assert out["status"] == "approved" and out["approval_override"] is False


def test_an_override_on_a_good_packet_is_not_recorded_as_one(owner):
    """Passing override on a packet that did not need it must not mark the application as waived."""
    out = approve(make_packet(owner, 80), override=True, note="not needed").json()
    assert out["approval_override"] is False


def test_review_itself_cannot_be_skipped(owner):
    """An unscored packet has nothing to weigh up, so the override does not apply to it."""
    job_id = make_packet(owner, None)
    response = approve(job_id, override=True)
    assert response.status_code == 400 and "Review the tailored resume" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "tailored"


def test_a_job_with_no_packet_at_all_is_refused(owner):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="override-test", source_ref=uuid.uuid4().hex, title="Lead SWE",
                  company="Logitech", status="tailored", state=S.READY_TO_APPLY)
        db.add(job)
        db.commit()
        job_id = job.id
    assert approve(job_id, override=True).status_code == 400


def test_the_screen_is_told_what_was_waived(owner):
    job_id = make_packet(owner, 45)
    assert client.get(f"/api/approvals/{job_id}").json()["approval"] is None
    approve(job_id, override=True, note="Checked by hand")

    packet = client.get(f"/api/approvals/{job_id}").json()
    assert packet["approval"] == {"override": True, "note": "Checked by hand"}
    assert packet["min_review_score"] == MINIMUM
    row = next(r for r in client.get("/api/approvals").json() if r["id"] == job_id)
    assert row["approval_override"] is True


# ───────────── the queue waits for the review ─────────────

def listed(job_id):
    return any(r["id"] == job_id for r in client.get("/api/approvals").json())


def approvals_badge():
    return client.get("/api/workspace/summary").json()["counts"]["approvals"]


def test_a_packet_reaches_approvals_only_once_it_is_reviewed(owner):
    before = approvals_badge()
    job_id = make_packet(owner, None)  # tailoring saved it; the Reviewer has not stored a score yet
    assert not listed(job_id), "still being reviewed: it stays on Tailor"
    assert approvals_badge() == before
    assert any(r["id"] == job_id for r in client.get("/api/tailor/queue").json()), "Tailor still shows it"

    with SessionLocal() as db:
        resume = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id))
        resume.reviewer_score = 80
        db.commit()
    assert listed(job_id) and approvals_badge() == before + 1


def test_a_new_version_under_review_leaves_the_queue_until_it_is_reviewed(owner):
    job_id = make_packet(owner, 80)
    assert listed(job_id)
    with SessionLocal() as db:
        db.add(ResumeVariant(job_id=job_id, user_id=owner, version=2, markdown="# Rohan v2", reviewer_score=None))
        db.commit()
    assert not listed(job_id), "the latest version decides, not an older reviewed one"


def test_an_approved_job_stays_listed(owner):
    job_id = make_packet(owner, 80)
    approve(job_id)
    assert listed(job_id)
