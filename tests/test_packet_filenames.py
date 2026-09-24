"""A downloaded packet file is called what the apply kit said it would be called.

The kit and the three download endpoints used to build their own names, so the screen promised
`rohan-roy-straive-v1.pdf` and the browser saved `job-282-resume.pdf`. One definition now serves both.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import Application, CandidateProfile, Job, ResumeVariant
from app.services.lifecycle import WorkflowState as S
from app.settings import get_settings
from app.workspace import packet_filename, packet_slug
from tests.conftest import ensure_test_user

client = TestClient(app)
DOWNLOADS = {"resume.pdf": "pdf", "resume.docx": "docx", "cover-letter.txt": "cover_letter"}


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture
def owner():
    user_id = ensure_test_user().id
    with SessionLocal() as db:
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
        if profile is None:
            profile = CandidateProfile(user_id=user_id, master_resume="x")
            db.add(profile)
        profile.name = "Rohan Roy"
        db.commit()
    return user_id


@pytest.fixture(autouse=True)
def clean(owner):
    """Remove this file's jobs and everything hanging off them.

    SQLite does not enforce the ON DELETE CASCADE the schema declares, so deleting only the jobs would leave
    orphaned applications and resumes that the next reused row id would pick up.
    """
    with SessionLocal() as db:
        job_ids = list(db.scalars(select(Job.id).where(Job.user_id == owner, Job.source == "filename-test")).all())
        if job_ids:
            db.execute(delete(Application).where(Application.job_id.in_(job_ids)))
            db.execute(delete(ResumeVariant).where(ResumeVariant.job_id.in_(job_ids)))
            db.execute(delete(Job).where(Job.id.in_(job_ids)))
        db.commit()
    yield


def make_packet(owner, company="Straive", version=1):
    with SessionLocal() as db:
        job = Job(user_id=owner, source="filename-test", source_ref=uuid.uuid4().hex, title="Lead Software Engineer",
                  company=company, status="approved", state=S.READY_TO_APPLY, description="x" * 300)
        db.add(job)
        db.flush()
        resume = ResumeVariant(job_id=job.id, user_id=owner, version=version, markdown="# Rohan Roy\n\n- Led teams",
                               cover_letter="Dear team, I am writing about the role.", reviewer_score=85)
        db.add(resume)
        db.flush()
        db.add(Application(job_id=job.id, user_id=owner, status="approved", approved_resume_id=resume.id))
        db.commit()
        return job.id


def kit_names(job_id):
    return {f["kind"]: f["name"] for f in client.get(f"/api/jobs/{job_id}/apply-kit").json()["files"]}


def downloaded_name(job_id, path):
    response = client.get(f"/api/jobs/{job_id}/{path}")
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="') and disposition.endswith('"')
    return disposition[len('attachment; filename="'):-1]


def test_what_the_kit_shows_is_what_the_browser_saves(owner):
    job_id = make_packet(owner)
    shown = kit_names(job_id)
    assert shown == {"PDF": "rohan-roy-straive-v1.pdf", "DOCX": "rohan-roy-straive-v1.docx",
                     "TXT": "cover-letter-rohan-roy-straive.txt"}
    assert downloaded_name(job_id, "resume.pdf") == shown["PDF"]
    assert downloaded_name(job_id, "resume.docx") == shown["DOCX"]
    assert downloaded_name(job_id, "cover-letter.txt") == shown["TXT"]
    for path in DOWNLOADS:
        assert "job-" not in downloaded_name(job_id, path), "the old generic name is gone"


def test_the_version_in_the_name_follows_the_packet(owner):
    job_id = make_packet(owner, version=3)
    assert kit_names(job_id)["PDF"] == "rohan-roy-straive-v3.pdf"
    assert downloaded_name(job_id, "resume.pdf") == "rohan-roy-straive-v3.pdf"


def test_the_company_in_the_name_follows_the_job(owner):
    job_id = make_packet(owner, company="Acme Corp.")
    assert kit_names(job_id)["PDF"] == "rohan-roy-acme-corp-v1.pdf"
    assert downloaded_name(job_id, "resume.docx") == "rohan-roy-acme-corp-v1.docx"


@pytest.mark.parametrize(("name", "company", "expected"), [
    ("Rohan Roy", "Straive", "rohan-roy-straive"),
    ("Rohan  Roy", "Straive / EU", "rohan-roy-straive-eu"),
    ("Rohan Roy", "Кто-то", "rohan-roy"),           # nothing usable from the company falls back to the name
    ("", "", "resume"),                              # and nothing usable at all still gives a valid file name
    ("Ann-Marie O'Neill", "AT&T", "ann-marie-o-neill-at-t"),
    ("x" * 120, "Straive", "x" * 80),                # long names are cut, never left dangling on a dash
])
def test_a_file_name_is_always_safe_to_write(name, company, expected):
    slug = packet_slug(name, company)
    assert slug == expected
    assert slug and not slug.startswith("-") and not slug.endswith("-")
    assert all(c.isalnum() or c == "-" for c in slug)


def test_each_part_of_a_packet_is_named_for_what_it_is():
    assert packet_filename("rohan-roy-straive", "pdf", 2) == "rohan-roy-straive-v2.pdf"
    assert packet_filename("rohan-roy-straive", "docx", 2) == "rohan-roy-straive-v2.docx"
    assert packet_filename("rohan-roy-straive", "cover_letter") == "cover-letter-rohan-roy-straive.txt"
    assert packet_filename("rohan-roy-straive", "pdf") == "rohan-roy-straive-v1.pdf"


def test_a_download_works_before_the_packet_is_approved(owner):
    """The kit needs an approved packet, but a download may be asked for straight from the tailor screen."""
    with SessionLocal() as db:
        job = Job(user_id=owner, source="filename-test", source_ref=uuid.uuid4().hex, title="Lead SWE",
                  company="Straive", status="tailored", state=S.REVIEWED, description="x" * 300)
        db.add(job)
        db.flush()
        db.add(ResumeVariant(job_id=job.id, user_id=owner, version=2, markdown="# Rohan", cover_letter="Dear team"))
        db.commit()
        job_id = job.id
    assert downloaded_name(job_id, "resume.pdf") == "rohan-roy-straive-v2.pdf"
    assert downloaded_name(job_id, "cover-letter.txt") == "cover-letter-rohan-roy-straive.txt"
