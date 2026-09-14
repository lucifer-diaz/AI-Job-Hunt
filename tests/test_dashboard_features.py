import io
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_dashboard_has_workspace_navigation():
    response = client.get("/")
    assert response.status_code == 200
    for label in ("Overview", "Today", "Discovery", "Tailor", "Approvals", "Tracker", "Knowledge bank", "Settings"):
        assert label in response.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert 'data-tag-input="roles"' in script.text
    assert 'data-tag-input="locations"' in script.text
    assert "Press Enter to add" in script.text


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


def test_ai_settings_persist_and_blank_values_inherit_env():
    base = {"application_mode": "manual_approval", "min_review_score": 70}
    assert client.put("/api/settings", json={**base, "stage_models": {"bogus": "x"}}).status_code == 422
    try:
        saved = client.put("/api/settings", json={
            **base, "llm_provider": "openrouter", "model": "",
            "stage_models": {"tailor": "vendor/tailor:free", "review": ""}, "require_free": False}).json()
        assert saved["ai"]["saved"]["stage_models"] == {"tailor": "vendor/tailor:free"}
        assert saved["ai"]["saved"]["require_free"] is False
        prefs = {s["id"]: s for s in client.get("/api/preferences").json()["stage_models"]}
        assert prefs["tailor"]["model"] == "vendor/tailor:free" and prefs["tailor"]["source"] == "settings"
        assert prefs["review"]["source"] in {"env", "default"}
        assert client.get("/api/preferences").json()["require_free"] is False
    finally:
        # Blank everything so other tests keep using their .env / monkeypatched provider.
        cleared = client.put("/api/settings", json=base).json()
        assert cleared["ai"]["saved"] == {"llm_provider": "", "model": "", "stage_models": {}, "require_free": None}


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


def test_discovery_limits_locations_to_five():
    response = client.post("/api/search/apify", json={
        "search_terms": ["Engineer"],
        "locations": [f"Location {index}" for index in range(6)],
    })
    assert response.status_code == 422


def test_discovery_starts_one_run_per_location(monkeypatch):
    started = []

    def fake_start(_db, payload):
        started.append(payload["location"])
        index = len(started)
        return SimpleNamespace(id=index, external_run_id=f"run-{index}",
                               actor_id="actor", status="READY")

    monkeypatch.setattr("app.main.start_apify_search", fake_start)
    response = client.post("/api/search/apify", json={
        "search_terms": ["AI Engineer", "ML Engineer"],
        "locations": ["Singapore", "Remote"],
    })
    assert response.status_code == 200
    assert started == ["Singapore", "Remote"]
    assert [run["location"] for run in response.json()["runs"]] == started
