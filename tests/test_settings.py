from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ApplicationSettings
from app.services.llm import LLMClient, LLMError
from app.settings import Settings


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def valid_payload(**overrides):
    payload = {
        "active_provider": "openrouter",
        "openrouter_model": "openrouter/free",
        "openai_model": "gpt-5.6-luna",
        "anthropic_model": "claude-sonnet-5",
        "approval_mode": "manual",
        "min_fit_score": 72,
        "followup_days": 5,
        "max_jobs_per_batch": 30,
    }
    payload.update(overrides)
    return payload


def test_openrouter_is_environment_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings(_env_file=None).llm_provider == "openrouter"


def test_settings_reject_unsupported_provider_and_invalid_model():
    client = TestClient(app)
    assert (
        client.put("/api/settings", json=valid_payload(active_provider="not-real")).status_code
        == 422
    )
    assert client.put("/api/settings", json=valid_payload(openrouter_model="  ")).status_code == 422
    assert (
        client.put("/api/settings", json=valid_payload(openai_model="bad model")).status_code == 422
    )


def test_provider_switch_is_persisted_and_used(monkeypatch):
    db = SessionLocal()
    db.add(ApplicationSettings(id=1, **valid_payload(active_provider="anthropic")))
    db.commit()
    llm = LLMClient(db)
    monkeypatch.setattr(
        llm,
        "_anthropic",
        lambda system, user, preferences: {
            "provider": "anthropic",
            "model": preferences.anthropic_model,
        },
    )
    assert llm.generate_json("system", "user") == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
    }
    db.close()


def test_missing_provider_credentials_has_explicit_error():
    client = LLMClient()
    client.settings = SimpleNamespace(llm_provider="openrouter", openrouter_api_key=None)
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY is missing"):
        client.generate_json("system", "user")


def test_settings_response_never_leaks_complete_secret(monkeypatch):
    import app.main as main

    secret = "sk-or-super-secret-value-1234"
    monkeypatch.setattr(main.settings, "openrouter_api_key", secret)
    response = TestClient(app).get("/api/settings")
    assert response.status_code == 200
    serialized = response.text
    assert secret not in serialized
    assert "super-secret-value" not in serialized
    state = response.json()["credentials"]["openrouter"]
    assert state == {"configured": True, "masked_suffix": "••••1234"}


def test_connection_test_reports_missing_credentials(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.settings, "openrouter_api_key", None)
    client = TestClient(app)
    client.put("/api/settings", json=valid_payload())
    response = client.post("/api/settings/test")
    assert response.status_code == 400
    assert response.json()["detail"] == "OPENROUTER_API_KEY is missing"
