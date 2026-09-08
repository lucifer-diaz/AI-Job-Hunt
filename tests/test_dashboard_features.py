import io

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_dashboard_has_workspace_navigation():
    response = client.get("/")
    assert response.status_code == 200
    for label in ("Discovery", "Approvals", "Knowledge bank", "Settings"):
        assert label in response.text


def test_settings_validation_and_secret_status():
    invalid = client.put("/api/settings", json={
        "llm_provider": "unknown", "model": "x",
        "application_mode": "manual_approval", "min_review_score": 70,
    })
    assert invalid.status_code == 422
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert "configured" in response.json()
    assert "api_key" not in response.text.lower()


def test_resume_upload_failure_cases_and_text_success():
    unsupported = client.post("/api/profile/resume", files={
        "file": ("resume.exe", io.BytesIO(b"bad"), "application/octet-stream")})
    assert unsupported.status_code == 415
    empty = client.post("/api/profile/resume", files={
        "file": ("resume.txt", io.BytesIO(b""), "text/plain")})
    assert empty.status_code == 400
    uploaded = client.post("/api/profile/resume", files={
        "file": ("../resume.txt", io.BytesIO(b"Python engineering experience"), "text/plain")})
    assert uploaded.status_code == 200
    assert uploaded.json()["filename"] == "resume.txt"
    assert client.get("/api/profile/resume").content == b"Python engineering experience"


def test_knowledge_conflict_requires_explicit_verification():
    question = "Are you legally authorized to work?"
    first = client.post("/api/knowledge", json={
        "question": question, "answer": "Yes", "category": "work_authorization",
        "answer_type": "boolean", "verified": True, "sensitive": True})
    assert first.status_code == 200
    conflict = client.post("/api/knowledge", json={
        "question": question.upper(), "answer": "No", "category": "work_authorization",
        "answer_type": "boolean", "verified": False, "sensitive": True})
    assert conflict.status_code == 409


def test_missing_application_detail_is_404():
    assert client.get("/api/jobs/999999/application-detail").status_code == 404
