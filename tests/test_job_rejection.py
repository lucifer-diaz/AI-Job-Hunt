"""Turning a job down from the approval queue.

Rejecting rejects the *job*, not the packet: it leaves the queue and the pipeline. Sending a packet back for
another tailoring pass is /revision, and that still keeps the job. The reason is optional throughout.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Application, Job, ResumeVariant, WorkflowTransition
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from app.workspace import REJECTION_REASONS
from tests.conftest import ensure_test_user, make_user

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    with SessionLocal() as db:
        db.execute(delete(Job).where(Job.user_id == owner, Job.source == "reject-test"))
        db.commit()
    yield


def make_job(owner, *, status="tailored", state=S.READY_TO_APPLY, applied=False):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="reject-test", source_ref=uuid.uuid4().hex, title="Lead Software Engineer",
                  company="Logitech", status=status, state=state, description="Python and leadership")
        db.add(job)
        db.flush()
        db.add(ResumeVariant(job_id=job.id, user_id=owner, version=1, markdown="# Rohan", reviewer_score=80))
        if applied:
            db.add(Application(job_id=job.id, user_id=owner, status="applied"))
        db.commit()
        return job.id


def reject(job_id, **body):
    return client.post(f"/api/jobs/{job_id}/reject", json=body)


def test_a_job_can_be_rejected_with_a_reason(owner):
    job_id = make_job(owner)
    out = reject(job_id, reason="compensation", notes="Band tops out below my range").json()
    assert out["job"]["status"] == "dismissed"
    assert out["rejection"]["reason"] == "compensation"
    assert out["rejection"]["label"] == "Pay too low"
    assert out["rejection"]["note"] == "Band tops out below my range" and out["rejection"]["at"]

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.rejection_reason == "compensation" and job.rejected_at is not None
        assert job.state is S.SKIPPED and job.previous_state is S.READY_TO_APPLY
        moved = db.scalar(select(WorkflowTransition).where(WorkflowTransition.job_id == job_id,
                                                           WorkflowTransition.to_state == S.SKIPPED))
        assert moved is not None and "Pay too low" in moved.reason and moved.actor == "user"


def test_a_job_can_be_rejected_without_one(owner):
    """One click has to be enough when you just want it gone."""
    job_id = make_job(owner)
    out = reject(job_id).json()
    assert out["job"]["status"] == "dismissed"
    assert out["rejection"]["reason"] is None and out["rejection"]["label"] == "No reason given"
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.rejection_reason is None and job.rejection_note is None and job.rejected_at is not None


def test_a_rejected_job_leaves_the_approval_queue(owner):
    keep, drop = make_job(owner), make_job(owner)
    before = {row["id"] for row in client.get("/api/approvals").json()}
    assert {keep, drop} <= before

    reject(drop, reason="location")
    after = client.get("/api/approvals").json()
    assert drop not in {row["id"] for row in after} and keep in {row["id"] for row in after}


def test_the_reason_travels_with_the_job(owner):
    """The rejection shows wherever the job appears afterwards, not only on the screen that recorded it."""
    job_id = make_job(owner)
    reject(job_id, reason="company", notes="Heard poor things")
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["rejection"]["label"] == "Not interested in the company"
    assert detail["rejection"]["note"] == "Heard poor things"

    listed = client.get("/api/jobs/search", params={"q": "Logitech", "stage": "all"}).json()["items"]
    rejected = next(row for row in listed if row["id"] == job_id)
    assert rejected["rejection"]["reason"] == "company"
    # A job nobody rejected carries nothing.
    assert client.get(f"/api/jobs/{make_job(owner)}").json()["rejection"] is None


def test_an_approved_packet_can_still_be_rejected_before_it_is_sent(owner):
    job_id = make_job(owner, status="approved")
    assert reject(job_id, reason="no_longer_open").status_code == 200
    with SessionLocal() as db:
        application = db.scalar(select(Application).where(Application.job_id == job_id))
        assert db.get(Job, job_id).status == "dismissed"
        assert application is None or application.status == "rejected"


def test_a_submitted_application_is_never_rewritten(owner):
    """Once it is out of the door the tracker owns the outcome, so rejecting cannot erase that it was sent."""
    job_id = make_job(owner, status="applied", state=S.APPLIED, applied=True)
    response = reject(job_id, reason="company")
    assert response.status_code == 409 and "tracker" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "applied" and db.get(Job, job_id).rejected_at is None


def test_an_unknown_reason_is_refused(owner):
    job_id = make_job(owner)
    assert reject(job_id, reason="i just dont like it").status_code == 422
    assert reject(job_id, reason="compensation", notes="x" * 3000).status_code == 422
    with SessionLocal() as db:
        assert db.get(Job, job_id).rejected_at is None, "a refused rejection changes nothing"


def test_the_offered_reasons_are_the_ones_the_api_accepts(owner):
    offered = {row["id"] for row in client.get("/api/rejection-reasons").json()["reasons"]}
    assert offered == set(REJECTION_REASONS)
    packet = client.get(f"/api/approvals/{make_job(owner)}").json()
    assert {row["id"] for row in packet["rejection_reasons"]} == offered, "the screen offers the same list"
    for reason in offered:
        assert reject(make_job(owner), reason=reason).status_code == 200


def test_a_job_keeps_its_tailored_work(owner):
    """Rejecting is not deleting: the packet stays, so a restored job is not tailored from scratch."""
    job_id = make_job(owner)
    reject(job_id, reason="seniority")
    with SessionLocal() as db:
        assert db.scalars(select(ResumeVariant).where(ResumeVariant.job_id == job_id)).all()


def test_one_account_cannot_reject_anothers_job(owner):
    theirs = make_user(SessionLocal, email=f"other-{uuid.uuid4().hex[:8]}@example.com")
    job_id = make_job(theirs)
    assert reject(job_id, reason="company").status_code == 404
    with SessionLocal() as db:
        assert db.get(Job, job_id).rejected_at is None


# ───────────── several at once ─────────────

def bulk(action, ids, **body):
    return client.post("/api/approvals/bulk", json={"action": action, "job_ids": ids, **body})


def test_several_jobs_can_be_rejected_at_once(owner):
    first, second = make_job(owner), make_job(owner, status="approved")
    out = bulk("reject", [first, second], reason="location", notes="Onsite only").json()
    assert out["done"] == [first, second] and out["skipped"] == []
    with SessionLocal() as db:
        for job_id in (first, second):
            job = db.get(Job, job_id)
            assert job.status == "dismissed" and job.rejection_reason == "location"
            assert job.rejection_note == "Onsite only" and job.state is S.SKIPPED
    assert not {first, second} & {row["id"] for row in client.get("/api/approvals").json()}


def test_one_job_that_cannot_be_rejected_never_blocks_the_rest(owner):
    """A submitted application, another account's job or a missing one is skipped with the reason."""
    theirs = make_job(make_user(SessionLocal, email=f"other-{uuid.uuid4().hex[:8]}@example.com"))
    sent = make_job(owner, status="applied", state=S.APPLIED, applied=True)
    fine = make_job(owner)
    out = bulk("reject", [sent, theirs, fine, 0]).json()
    assert out["done"] == [fine]
    skipped = {row["id"]: row["reason"] for row in out["skipped"]}
    assert set(skipped) == {sent, theirs, 0}
    assert skipped[theirs] == "Job not found"
    with SessionLocal() as db:
        assert db.get(Job, sent).status == "applied" and db.get(Job, theirs).rejected_at is None


def test_a_bulk_reject_checks_its_reason_before_touching_anything(owner):
    job_id = make_job(owner)
    assert bulk("reject", [job_id], reason="nope").status_code == 422
    assert bulk("reject", []).status_code == 422
    assert bulk("archive", [job_id]).status_code == 422
    with SessionLocal() as db:
        assert db.get(Job, job_id).rejected_at is None


def test_several_packets_can_be_sent_back_for_revision(owner):
    first, second = make_job(owner), make_job(owner, status="approved")
    already = make_job(owner, status="pending_approval")
    out = bulk("revise", [first, second, already]).json()
    assert out["done"] == [first, second]
    assert out["skipped"] == [{"id": already, "title": "Lead Software Engineer", "reason": "Already sent back for revision"}]
    with SessionLocal() as db:
        for job_id in (first, second):
            assert db.get(Job, job_id).status == "pending_approval"
            assert db.scalar(select(Application).where(Application.job_id == job_id)).status == "pending_approval"


def test_bulk_approval_never_waives_the_reviewer_minimum(owner):
    cleared = make_job(owner)  # reviewer 80
    low = make_job(owner)
    with SessionLocal() as db:
        db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == low)).reviewer_score = 1
        db.commit()
    out = bulk("approve", [cleared, low]).json()
    assert out["done"] == [cleared]
    assert len(out["skipped"]) == 1 and "below your minimum" in out["skipped"][0]["reason"]
    with SessionLocal() as db:
        assert db.get(Job, cleared).status == "approved" and db.get(Job, low).status == "tailored"
        approved = db.scalar(select(Application).where(Application.job_id == cleared))
        assert approved.status == "approved" and not approved.approval_override
