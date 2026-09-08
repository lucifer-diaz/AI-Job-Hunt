import os

os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"

from fastapi.testclient import TestClient
from app.main import app


def test_health_and_dashboard():
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    r = client.get("/")
    assert r.status_code == 200
    assert "Job Hunt OS" in r.text


def test_automatic_mode_requires_explicit_confirmation():
    client = TestClient(app)
    payload = {
        "application_mode": "automatic",
        "min_fit_score": 80,
        "min_resume_score": 75,
        "excluded_companies": [],
        "excluded_locations": [],
    }
    assert client.put("/api/application-settings", json=payload).status_code == 422
    payload["confirm_automatic"] = True
    response = client.put("/api/application-settings", json=payload)
    assert response.status_code == 200
    assert client.get("/api/application-settings").json()["application_mode"] == "automatic"
