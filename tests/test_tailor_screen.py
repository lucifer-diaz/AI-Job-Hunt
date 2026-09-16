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


def test_uploading_and_choosing_a_resume_decides_what_tailoring_uses():
    first = f"Alpha resume {uuid.uuid4().hex[:8]}"
    second = f"Beta resume {uuid.uuid4().hex[:8]}"
    older = upload("alpha.txt", first).json()["id"]
    newer = upload("beta.txt", second).json()["id"]
    assert master_resume() == second, "the newest upload is what tailoring uses"

    resumes = {r["id"]: r for r in client.get("/api/profile/resumes").json()}
    assert resumes[newer]["active"] and resumes[newer]["in_use"] and not resumes[older]["in_use"]

    client.post(f"/api/profile/resumes/{older}/primary")
    assert master_resume() == first
    resumes = {r["id"]: r for r in client.get("/api/profile/resumes").json()}
    assert resumes[older]["in_use"] and not resumes[newer]["in_use"]


def test_a_stale_profile_form_cannot_overwrite_a_newer_resume():
    text = f"Gamma resume {uuid.uuid4().hex[:8]}"
    upload("gamma.txt", text)
    profile = client.get("/api/profile").json()
    profile.pop("master_resume")  # the browser is on a page loaded before the upload

    assert client.put("/api/profile", json={**profile, "name": "Rohan"}).status_code == 200
    assert master_resume() == text

    assert client.put("/api/profile", json={**profile, "master_resume": "hand written"}).status_code == 200
    assert master_resume() == "hand written", "an explicit edit still wins"


def test_deleting_a_resume_promotes_the_next_one_and_removes_the_file():
    from pathlib import Path

    first, second = f"Delta {uuid.uuid4().hex[:8]}", f"Epsilon {uuid.uuid4().hex[:8]}"
    older = upload("delta.txt", first).json()["id"]
    newer = upload("epsilon.txt", second).json()["id"]
    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        from app.models import ResumeDocument
        stored = Path(db.get(ResumeDocument, newer).storage_path)
    assert stored.is_file()

    out = client.delete(f"/api/profile/resumes/{newer}")
    assert out.status_code == 200 and out.json()["promoted"] == older
    assert master_resume() == first, "deleting the primary falls back to the next newest"
    assert not stored.exists()
    assert newer not in [r["id"] for r in client.get("/api/profile/resumes").json()]

    assert client.delete(f"/api/profile/resumes/{older}").status_code == 200
    assert client.delete(f"/api/profile/resumes/{older}").status_code == 404
