import os
os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_jobhunt.db")

from app.db import Base, SessionLocal, engine
from app.models import Evidence, Job
from app.services.workflow import approve_job, get_profile, mark_applied, rank_job, tailor_job


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_core_happy_path():
    from tests.conftest import ensure_test_user

    db = SessionLocal()
    db.info["user_id"] = ensure_test_user().id  # services act for the account the session belongs to
    p = get_profile(db)
    p.name = "Test Candidate"
    p.headline = "Senior Python Engineer"
    p.master_resume = "10 years Python, FastAPI, AWS, Kubernetes."
    p.target_roles = ["Senior Engineer"]
    db.add(Evidence(company="Acme", role="Lead", claim="Reduced incidents by 40%", skills=["Python", "AWS"]))
    job = Job(source="manual", source_ref="x", title="Senior Python Engineer", company="Example", description="Need Python FastAPI AWS")
    db.add(job)
    db.commit()
    db.refresh(job)

    result = rank_job(db, job)
    assert result["score"] == 80
    resume = tailor_job(db, job)
    assert resume.reviewer_score == 86
    app = approve_job(db, job)
    assert app.status == "approved"
    app = mark_applied(db, job)
    assert app.status == "applied"
    assert app.followup_due_at is not None
    db.close()
