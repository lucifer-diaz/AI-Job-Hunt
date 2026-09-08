import app.services.workflow as workflow
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Job, ResumeVariant


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def make_job(*, score=85, reviewed=True):
    db = SessionLocal()
    job = Job(
        source="manual",
        source_ref="approval-test",
        title="Platform Engineer",
        company="Acme",
        url="https://example.test/apply",
        description="Build platforms",
        fit_score=91,
        fit_analysis={"summary": "Strong fit"},
        raw={"ats_questions": [{"question": "Authorized?"}], "private_blob": "do not expose"},
        status="tailored",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    resume = ResumeVariant(
        job_id=job.id,
        version=1,
        markdown="# Resume",
        cover_letter="Hello",
        reviewer_score=score if reviewed else None,
        reviewer_notes={"strengths": ["Python"]},
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return db, job, resume


def test_approval_guards_and_pins_reviewed_resume():
    db, job, resume = make_job(reviewed=False)
    try:
        try:
            workflow.approve_job(db, job)
            assert False, "unreviewed resume was approved"
        except ValueError as exc:
            assert "completed review" in str(exc)
        resume.reviewer_score = 79
        db.commit()
        old_threshold = workflow.get_settings().min_review_score
        workflow.get_settings().min_review_score = 80
        try:
            try:
                workflow.approve_job(db, job)
                assert False, "below-threshold resume was approved"
            except ValueError as exc:
                assert "below the required" in str(exc)
            resume.reviewer_score = 80
            db.commit()
            application = workflow.approve_job(db, job)
            assert application.approved_resume_id == resume.id
            approved_at = application.approved_at
            assert workflow.approve_job(db, job).approved_at == approved_at
            newer = ResumeVariant(job_id=job.id, version=2, markdown="new", reviewer_score=None)
            db.add(newer)
            db.commit()
            assert workflow.approve_job(db, job).approved_resume_id == resume.id
        finally:
            workflow.get_settings().min_review_score = old_threshold
    finally:
        db.close()


def test_no_resume_and_application_transitions_are_validated_and_idempotent():
    db = SessionLocal()
    job = Job(source="manual", source_ref="none", title="Role", company="Co", description="x")
    db.add(job)
    db.commit()
    try:
        try:
            workflow.approve_job(db, job)
            assert False, "job without resume was approved"
        except ValueError as exc:
            assert "Tailor" in str(exc)
        rejected = workflow.change_application_state(db, job, "rejected", "Not now")
        assert workflow.change_application_state(db, job, "rejected").id == rejected.id
        try:
            workflow.change_application_state(db, job, "revision_requested")
            assert False, "invalid transition succeeded"
        except ValueError:
            pass
    finally:
        db.close()


def test_approval_api_detail_notes_revisions_and_repeated_applied():
    db, job, resume = make_job()
    job_id = job.id
    resume_id = resume.id
    db.close()
    client = TestClient(app)
    assert client.get("/api/jobs/999999/application-detail").status_code == 404
    detail = client.get(f"/api/jobs/{job_id}/application-detail")
    assert detail.status_code == 200
    payload = detail.json()
    assert set(payload) == {
        "job",
        "fit_analysis",
        "resume",
        "cover_letter",
        "reviewer_feedback",
        "application",
        "ats_questions",
    }
    assert payload["resume"]["id"] == resume_id
    assert payload["ats_questions"] == [{"question": "Authorized?"}]
    assert "private_blob" not in detail.text

    notes = {"recruiter_name": "Rae", "recruiter_email": "rae@example.test", "notes": "Call Friday"}
    assert client.put(f"/api/jobs/{job_id}/application", json=notes).status_code == 200
    assert (
        client.get(f"/api/jobs/{job_id}/application-detail").json()["application"]["notes"]
        == "Call Friday"
    )
    assert (
        client.post(f"/api/jobs/{job_id}/revision", json={"reason": "Tighten summary"}).json()[
            "status"
        ]
        == "revision_requested"
    )
    assert (
        client.post(f"/api/jobs/{job_id}/revision", json={"reason": "again"}).json()["status"]
        == "revision_requested"
    )
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/approve").status_code == 200
    assert (
        client.post(f"/api/jobs/{job_id}/applied", json={**notes, "submitted": False}).status_code
        == 422
    )
    first = client.post(f"/api/jobs/{job_id}/applied", json={**notes, "submitted": True})
    second = client.post(f"/api/jobs/{job_id}/applied", json={**notes, "submitted": True})
    assert first.status_code == second.status_code == 200
    assert first.json()["followup_due_at"] == second.json()["followup_due_at"]
    assert client.post(f"/api/jobs/{job_id}/reject", json={"reason": "late"}).status_code == 409
    assert len(client.get("/api/applications?status=applied").json()) == 1
    assert client.get("/api/applications?status=bogus").status_code == 422
    assert "Application approvals" in client.get("/approvals").text
