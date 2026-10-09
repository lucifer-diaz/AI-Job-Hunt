"""The Tailor screen's queue, and which resume tailoring actually uses."""
import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import CandidateProfile
from app.settings import get_settings
from tests.conftest import ensure_test_user

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def make_job(tag, title="Engineering Manager"):
    return client.post("/api/jobs/from-url", json={"url": f"https://example.com/tailor/{tag}", "title": title,
                                                   "company": f"Tailor Co {tag}", "description": "Python and Kafka"}
                       ).json()["job"]["id"]


def upload(name, text):
    return client.post("/api/profile/resume",
                       files={"file": (name, io.BytesIO(text.encode()), "text/plain")})


def master_resume():
    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        return db.query(CandidateProfile).filter_by(user_id=ensure_test_user().id).one().master_resume


def test_the_tailor_queue_carries_the_scores_the_bulk_table_shows():
    tag = uuid.uuid4().hex[:8]
    job_id = make_job(tag)
    client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})

    row = next(r for r in client.get("/api/tailor/queue").json() if r["id"] == job_id)
    assert row["has_resume"] is False and row["reviewer_score"] is None and row["ats_pct"] is None

    assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    row = next(r for r in client.get("/api/tailor/queue").json() if r["id"] == job_id)
    packet = client.get(f"/api/tailor/{job_id}").json()["resume"]
    assert row["has_resume"] is True and row["untailored"] is False
    assert row["reviewer_score"] == packet["reviewer_score"] and row["ats_pct"] == packet["ats_pct"]
    assert row["resume_version"] == packet["version"]


def test_skipped_tailoring_is_marked_in_the_queue():
    tag = uuid.uuid4().hex[:8]
    job_id = make_job(tag)
    client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{job_id}/skip-tailoring").status_code == 200

    row = next(r for r in client.get("/api/tailor/queue").json() if r["id"] == job_id)
    assert row["untailored"] is True and row["reviewer_score"] is not None


def test_uploading_never_changes_the_active_resume_choosing_one_does():
    before = master_resume()
    first = f"Alpha resume {uuid.uuid4().hex[:8]}"
    second = f"Beta resume {uuid.uuid4().hex[:8]}"
    older = upload("alpha.txt", first).json()["id"]
    newer_upload = upload("beta.txt", second).json()
    newer = newer_upload["id"]
    assert newer_upload["is_active"] is False and master_resume() in (before, first), \
        "an upload is a new version; the active resume is still the one you chose"

    client.post(f"/api/profile/resumes/{newer}/primary")
    assert master_resume() == second, "choosing a file makes its version the active resume"
    resumes = {r["id"]: r for r in client.get("/api/profile/resumes").json()}
    assert resumes[newer]["active"] and resumes[newer]["in_use"] and not resumes[older]["active"]

    client.post(f"/api/profile/resumes/{older}/primary")
    assert master_resume() == first
    resumes = {r["id"]: r for r in client.get("/api/profile/resumes").json()}
    assert resumes[older]["in_use"] and not resumes[newer]["in_use"]
    active = client.get("/api/settings/resume").json()["active"]
    assert active["id"] == resumes[older]["version_id"], "the library and Resume Data name the same active resume"


def test_a_stale_profile_form_cannot_change_the_active_resume():
    text = f"Gamma resume {uuid.uuid4().hex[:8]}"
    client.post(f"/api/profile/resumes/{upload('gamma.txt', text).json()['id']}/primary")
    profile = client.get("/api/profile").json()
    stale = {**profile, "master_resume": "text the page loaded before the upload"}

    assert client.put("/api/profile", json={**profile, "name": "Rohan"}).status_code == 200
    assert master_resume() == text
    saved = client.put("/api/profile", json={**stale, "name": "Rohan"}).json()
    assert master_resume() == text, "text from an old page becomes its own version, never the active one"
    assert saved["created_resume_active"] is False

    assert client.post(f"/api/resumes/{saved['created_resume_id']}/activate").status_code == 200
    assert master_resume() == stale["master_resume"], "it is used once it is made active"


def test_deleting_a_file_never_changes_the_active_resume_and_keeps_the_original():
    from pathlib import Path

    first, second = f"Delta {uuid.uuid4().hex[:8]}", f"Epsilon {uuid.uuid4().hex[:8]}"
    older = upload("delta.txt", first).json()["id"]
    newer = upload("epsilon.txt", second).json()["id"]
    client.post(f"/api/profile/resumes/{newer}/primary")
    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        from app.models import ResumeDocument
        stored = Path(db.get(ResumeDocument, newer).storage_path)
    assert stored.is_file()

    out = client.delete(f"/api/profile/resumes/{newer}")
    assert out.status_code == 200 and out.json()["promoted"] is None
    assert master_resume() == second, "the active resume is a version: removing its file does not change it"
    assert stored.exists(), "a resume version still references its original upload, so the file is kept"
    assert newer not in [r["id"] for r in client.get("/api/profile/resumes").json()]

    assert client.delete(f"/api/profile/resumes/{older}").status_code == 200
    assert client.delete(f"/api/profile/resumes/{older}").status_code == 404
