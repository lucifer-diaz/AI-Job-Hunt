import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

import app.main as main
from app.db import SessionLocal
from app.models import CandidateProfile, ResumeDocument
from app.services.documents import LocalDocumentStorage


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "document_storage", LocalDocumentStorage(tmp_path))
    monkeypatch.setattr(main.settings, "resume_max_upload_bytes", 64)
    monkeypatch.setattr(main.settings, "app_username", None)
    monkeypatch.setattr(main.settings, "app_password", None)
    with SessionLocal() as db:
        db.execute(delete(ResumeDocument))
        profile = db.get(CandidateProfile, 1)
        if profile:
            profile.master_resume = ""
            profile.search_preferences = {"keep": True}
        db.commit()
    return TestClient(main.app)


def upload(client, name="resume.txt", content=b"Senior engineer\nBuilt reliable systems."):
    return client.post("/api/profile/resume", files={"file": (name, content, "text/plain")})


def test_upload_metadata_download_replace_and_delete(client):
    response = upload(client)
    assert response.status_code == 201
    metadata = response.json()
    assert metadata["original_filename"] == "resume.txt"
    assert metadata["size"] == 39
    assert metadata["extraction_status"] == "complete"
    assert len(metadata["checksum"]) == 64
    assert "storage_key" not in metadata

    assert client.get("/api/profile/resume").json()["original_filename"] == "resume.txt"
    download = client.get("/api/profile/resume/download")
    assert download.status_code == 200
    assert download.content == b"Senior engineer\nBuilt reliable systems."

    replacement = client.put(
        "/api/profile/resume", files={"file": ("new.md", b"# Updated resume", "text/markdown")}
    )
    assert replacement.status_code == 200
    assert replacement.json()["extracted_text"] == "# Updated resume"
    assert client.delete("/api/profile/resume").status_code == 204
    assert client.get("/api/profile/resume").status_code == 404


def test_rejects_oversize_unsupported_disguised_and_unsafe_files(client):
    assert upload(client, content=b"x" * 65).status_code == 413
    assert client.post(
        "/api/profile/resume", files={"file": ("resume.exe", b"hello", "application/octet-stream")}
    ).status_code == 422
    assert client.post(
        "/api/profile/resume", files={"file": ("resume.pdf", b"plain text", "application/pdf")}
    ).status_code == 422
    assert upload(client, "../resume.txt").status_code == 422
    assert upload(client, "..\\resume.txt").status_code == 422


def test_failed_extraction_does_not_replace_current_document(client):
    assert upload(client).status_code == 201
    before = client.get("/api/profile/resume", params={"include_text": True}).json()
    failed = client.put(
        "/api/profile/resume",
        files={"file": ("broken.pdf", b"%PDF-not-a-real-pdf", "application/pdf")},
    )
    assert failed.status_code == 422
    after = client.get("/api/profile/resume", params={"include_text": True}).json()
    assert after["checksum"] == before["checksum"]
    assert after["extracted_text"] == before["extracted_text"]


def test_download_requires_configured_authentication(client, monkeypatch):
    assert upload(client).status_code == 201
    monkeypatch.setattr(main.settings, "app_username", "user")
    monkeypatch.setattr(main.settings, "app_password", "secret")
    assert client.get("/api/profile/resume/download").status_code == 401
    token = base64.b64encode(b"user:secret").decode()
    assert client.get(
        "/api/profile/resume/download", headers={"Authorization": f"Basic {token}"}
    ).status_code == 200


def test_profile_patch_preserves_omitted_fields(client):
    response = client.patch("/api/profile", json={"headline": "New headline"})
    assert response.status_code == 200
    profile = client.get("/api/profile").json()
    assert profile["headline"] == "New headline"
    assert profile["search_preferences"] == {"keep": True}
