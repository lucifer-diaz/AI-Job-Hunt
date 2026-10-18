"""Starring a job on the Approvals screen: a bookmark that leads the queue and filters it, and changes nothing else."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Job, ResumeVariant
from app.services.lifecycle import WorkflowState as S
from tests.conftest import ensure_test_user

client = TestClient(app)


@pytest.fixture
def owner():
    user_id = ensure_test_user().id
    yield user_id
    with SessionLocal() as db:
        job_ids = list(db.scalars(select(Job.id).where(Job.user_id == user_id, Job.source == "star-test")).all())
        if job_ids:
            db.execute(delete(ResumeVariant).where(ResumeVariant.job_id.in_(job_ids)))
            db.execute(delete(Job).where(Job.id.in_(job_ids)))
        db.commit()


def make_packet(owner, title):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="star-test", source_ref=uuid.uuid4().hex, title=title,
                  company="Logitech", status="tailored", state=S.READY_TO_APPLY)
        db.add(job)
        db.flush()
        db.add(ResumeVariant(job_id=job.id, user_id=owner, version=1, markdown="# Rohan", reviewer_score=80))
        db.commit()
        return job.id


def mine(rows):
    return [r for r in rows if r["source"] == "star-test"]


def test_a_starred_job_leads_the_queue_and_can_be_filtered(owner):
    first, second = make_packet(owner, "First"), make_packet(owner, "Second")
    rows = mine(client.get("/api/approvals").json())
    assert [r["id"] for r in rows] == [second, first] and not any(r["starred"] for r in rows)

    response = client.put(f"/api/jobs/{first}/star", json={"starred": True})
    assert response.status_code == 200 and response.json() == {"ok": True, "starred": True}

    rows = mine(client.get("/api/approvals").json())
    assert [r["id"] for r in rows] == [first, second] and rows[0]["starred"] is True
    assert [r["id"] for r in mine(client.get("/api/approvals?status=starred").json())] == [first]
    assert client.get(f"/api/approvals/{first}").json()["job"]["starred"] is True


def test_starring_changes_no_workflow_state(owner):
    job_id = make_packet(owner, "Only")
    client.put(f"/api/jobs/{job_id}/star", json={"starred": True})
    client.put(f"/api/jobs/{job_id}/star", json={"starred": False})
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.starred is False and job.status == "tailored" and job.state == S.READY_TO_APPLY


def test_starring_needs_a_value(owner):
    job_id = make_packet(owner, "Only")
    assert client.put(f"/api/jobs/{job_id}/star", json={}).status_code == 422
