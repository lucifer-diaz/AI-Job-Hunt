"""Every user-owned row names its owner, and the ORM can navigate between a user and their data."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Application, ApplicationEvent, Job, ResumeVariant, User, WorkflowTransition
from app.services import work_queue as wq
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from app.tenancy import OWNED_MODELS
from tests.conftest import ensure_test_user, make_user
from tests.resume_support import use_resume_text

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def add_job(user_id, tag, **fields):
    with SessionLocal() as db:
        job = Job(user_id=user_id, source="manual", source_ref=f"own-{tag}", title="Engineering Manager",
                  company=f"Owned Co {tag}", state=S.DISCOVERED, **fields)
        db.add(job)
        db.commit()
        return job.id


@pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda m: m.__name__)
def test_every_owned_model_names_its_owner(model):
    assert "user_id" in model.__table__.columns, f"{model.__name__} has no owner column"
    assert not model.__table__.columns["user_id"].nullable, f"{model.__name__}.user_id must be required"
    assert model.__mapper__.relationships["user"].mapper.class_ is User


def test_a_user_reaches_their_data_through_relationships():
    tag = uuid.uuid4().hex[:8]
    mine = ensure_test_user().id
    theirs = make_user(SessionLocal, email=f"other-{tag}@example.com")
    my_job, their_job = add_job(mine, f"mine-{tag}"), add_job(theirs, f"theirs-{tag}")

    with SessionLocal() as db:  # no user bound: the relationships themselves must separate the accounts
        me, them = db.get(User, mine), db.get(User, theirs)
        assert my_job in [job.id for job in me.jobs] and their_job not in [job.id for job in me.jobs]
        assert [job.id for job in them.jobs] == [their_job] or their_job in [job.id for job in them.jobs]
        assert db.get(Job, my_job).user.id == mine, "a job knows who owns it"


def test_tailoring_through_the_api_owns_every_row_it_creates():
    tag = uuid.uuid4().hex[:8]
    use_resume_text(client, "Built Python FastAPI services at 10k rps")
    job = client.post("/api/jobs/from-url", json={"url": f"https://example.com/own/{tag}", "title": "Staff Engineer",
                                                  "company": f"Own Co {tag}", "description": "Python"}).json()["job"]
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    client.post(f"/api/jobs/{job['id']}/approve")

    owner = ensure_test_user().id
    with SessionLocal() as db:
        assert db.scalar(select(ResumeVariant.user_id).where(ResumeVariant.job_id == job["id"])) == owner
        assert db.scalar(select(Application.user_id).where(Application.job_id == job["id"])) == owner
        # Whatever workflow history this job has, every row of it belongs to the same account.
        owners = set(db.scalars(select(WorkflowTransition.user_id).where(WorkflowTransition.job_id == job["id"])).all())
        assert owners <= {owner}


def test_rows_created_without_a_signed_in_user_still_get_an_owner():
    """The scheduler and workers use sessions that belong to nobody; their rows inherit the job's owner."""
    tag = uuid.uuid4().hex[:8]
    owner = make_user(SessionLocal, email=f"worker-{tag}@example.com")
    job_id = add_job(owner, f"worker-{tag}")

    with SessionLocal() as db:
        assert db.info.get("user_id") is None
        job = db.get(Job, job_id)
        wq.transition(db, job, S.NORMALIZED, actor="test-worker", reason="worker path")
        db.commit()
        assert db.scalar(select(WorkflowTransition.user_id).where(WorkflowTransition.job_id == job_id)) == owner


def test_a_child_row_takes_its_owner_from_its_parent():
    tag = uuid.uuid4().hex[:8]
    owner = make_user(SessionLocal, email=f"parent-{tag}@example.com")
    job_id = add_job(owner, f"parent-{tag}")

    with SessionLocal() as db:
        application = Application(job_id=job_id, status="pending_approval")
        db.add(application)
        db.commit()
        assert application.user_id == owner, "an application inherits from its job"

        event = ApplicationEvent(application_id=application.id, kind="note", title="Recruiter called")
        db.add(event)
        db.commit()
        assert event.user_id == owner, "an event inherits through its application"


def test_one_account_never_sees_another_accounts_rows():
    tag = uuid.uuid4().hex[:8]
    theirs = make_user(SessionLocal, email=f"hidden-{tag}@example.com")
    their_job = add_job(theirs, f"hidden-{tag}")

    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        assert db.get(Job, their_job) is None
        assert db.scalar(select(Job.id).where(Job.id == their_job)) is None
        assert db.scalar(select(WorkflowTransition.id).where(WorkflowTransition.job_id == their_job)) is None
