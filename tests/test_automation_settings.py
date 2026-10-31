"""Settings → AI agents & automation: the agent and application configuration an admin sets from the app.

It overrides the environment without a restart (laid over the settings object, so every reader and every derived
property sees it), only an admin may change it, every value is one of its choices or checked, saving the environment's
value removes the override, and a secret's value is never returned.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import ServerSetting
from app.scheduler import build_scheduler
from app.services import application_execution, qualification, server_settings
from app.settings import get_settings
from tests.conftest import make_user

URL = "/api/settings/automation"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    settings = get_settings()
    # The environment these tests start from, whatever the developer's .env says.
    for name, value in (("agent_graphs_enabled", False), ("application_execution_mode", "off"),
                        ("qualification_graph_mode", "off"), ("browser_use_headless", True),
                        ("application_login_domains", ""), ("application_takeover_ttl_minutes", 15),
                        ("agent_langsmith_api_key", "lsv2-secret-not-shown-000000")):
        monkeypatch.setattr(settings, name, value)

    def clear():
        with SessionLocal() as db:
            db.execute(delete(ServerSetting))
            db.commit()
        server_settings.apply()
    clear()
    yield
    clear()


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(admin=False):
        uid = make_user(SessionLocal)
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", admin))
        return TestClient(app)
    return sign_in


def setting(view, name):
    return next(s for s in view["settings"] if s["name"] == name)


def test_everyone_reads_the_choices_and_only_an_admin_changes_them(signed_in):
    person = signed_in()
    view = person.get(URL).json()
    assert view["editable"] is False
    mode = setting(view, "application_execution_mode")
    assert mode["env"] == "APPLICATION_EXECUTION_MODE" and mode["value"] == "off" and mode["default"] == "off"
    assert [c["value"] for c in mode["choices"]] == ["off", "observe", "assist", "guarded_auto"]
    assert all(c["help"] for c in mode["choices"])  # every option says what it does
    assert {g["id"] for g in view["groups"]} == {s["group"] for s in view["settings"]}
    assert [p["id"] for p in view["presets"]] == ["manual", "read", "fill", "auto"]
    assert view["environment"]["langsmith_api_key"] is True and "lsv2-secret" not in str(view)
    key = setting(view, "agent_langsmith_api_key")  # whether a key is set and where, never the key
    assert key["kind"] == "secret" and key["value"] == "" and key["default"] == ""
    assert key["set"] is True and key["env_set"] is True and key["overridden"] is False
    assert person.put(URL, json={"values": {"application_execution_mode": "assist"}}).status_code == 403


def test_an_admins_choice_applies_at_once_everywhere_and_the_environment_value_removes_it(signed_in):
    admin = signed_in(admin=True)
    out = admin.put(URL, json={"values": {"agent_graphs_enabled": "true", "application_execution_mode": "assist",
                                          "qualification_graph_mode": "shadow",
                                          "browser_use_headless": "false"}})
    assert out.status_code == 200, out.text
    view = out.json()
    assert setting(view, "application_execution_mode")["value"] == "assist"
    assert setting(view, "application_execution_mode")["default"] == "off"
    assert setting(view, "application_execution_mode")["overridden"] is True
    settings = get_settings()
    # Every reader sees it, derived properties too, without a restart.
    assert settings.agent_graphs_enabled is True and settings.browser_use_headless is False
    assert application_execution.mode() == "assist" and qualification.mode() == "shadow"
    assert build_scheduler().interval("qualification") == settings.qualification_minutes  # follows the switch
    with SessionLocal() as db:
        assert db.get(ServerSetting, "agent_graphs_enabled").value == "true"
    # Saving the environment's value hands the setting back to the environment.
    admin.put(URL, json={"values": {"agent_graphs_enabled": "false"}}).raise_for_status()
    with SessionLocal() as db:
        assert db.get(ServerSetting, "agent_graphs_enabled") is None
    assert get_settings().agent_graphs_enabled is False and application_execution.mode() == "off"
    assert build_scheduler().interval("qualification") == 0


@pytest.mark.parametrize("values", [
    {"application_execution_mode": "autonomous"},
    {"agent_graphs_enabled": "maybe"},
    {"application_takeover_ttl_minutes": 0},
    {"application_login_domains": "acme.wd5.myworkdayjobs.com, not a host"},
    {"agent_langsmith_endpoint": "http://plain.example.com"},
    {"secret_key": "x"},
    {"browser_use_python": "/tmp/python"},
])
def test_anything_but_a_listed_choice_or_a_checked_value_is_refused_and_nothing_is_saved(signed_in, values):
    admin = signed_in(admin=True)
    out = admin.put(URL, json={"values": {"application_execution_mode": "assist", **values}})
    assert out.status_code == 422
    with SessionLocal() as db:
        assert db.scalars(select(ServerSetting)).all() == []  # all or none
    assert application_execution.mode() == "off"


def test_text_values_are_cleaned(signed_in):
    admin = signed_in(admin=True)
    view = admin.put(URL, json={"values": {
        "application_login_domains": " Acme.WD5.myworkdayjobs.com., careers.example.com,acme.wd5.myworkdayjobs.com",
        "application_takeover_ttl_minutes": "30"}}).json()
    assert setting(view, "application_login_domains")["value"] == "acme.wd5.myworkdayjobs.com,careers.example.com"
    assert get_settings().login_domains == frozenset({"acme.wd5.myworkdayjobs.com", "careers.example.com"})
    assert get_settings().application_takeover_ttl_minutes == 30


def test_the_langsmith_region_is_offered_and_any_https_address_still_works(signed_in):
    admin = signed_in(admin=True)
    region = setting(admin.get(URL).json(), "agent_langsmith_endpoint")
    assert [c["label"] for c in region["choices"]] == ["US", "EU", "APAC"]
    view = admin.put(URL, json={"values": {"agent_langsmith_endpoint": "https://apac.api.smith.langchain.com"}}).json()
    assert setting(view, "agent_langsmith_endpoint")["value"] == "https://apac.api.smith.langchain.com"
    assert get_settings().agent_langsmith_endpoint == "https://apac.api.smith.langchain.com"
    admin.put(URL, json={"values": {"agent_langsmith_endpoint": "https://langsmith.example.com/api/"}}).raise_for_status()
    assert get_settings().agent_langsmith_endpoint == "https://langsmith.example.com/api"  # self-hosted


def test_a_langsmith_key_saved_here_is_encrypted_used_and_never_returned(signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "test-secret-key-for-encryption")
    admin = signed_in(admin=True)
    view = admin.put(URL, json={"values": {"agent_langsmith_api_key": "  lsv2_pt_saved-in-the-app-1234  "}}).json()
    assert "saved-in-the-app" not in str(view)
    key = setting(view, "agent_langsmith_api_key")
    assert key["overridden"] is True and key["set"] is True and key["value"] == ""
    with SessionLocal() as db:
        stored = db.get(ServerSetting, "agent_langsmith_api_key").value
    assert "saved-in-the-app" not in stored  # encrypted at rest
    assert get_settings().agent_langsmith_api_key == "lsv2_pt_saved-in-the-app-1234"  # in force at once, trimmed
    assert "saved-in-the-app" not in admin.get(URL).text
    # Other settings saved later leave the key alone; an empty value removes it and the environment's applies.
    admin.put(URL, json={"values": {"agent_langsmith_project": "elsewhere"}}).raise_for_status()
    assert get_settings().agent_langsmith_api_key == "lsv2_pt_saved-in-the-app-1234"
    view = admin.put(URL, json={"values": {"agent_langsmith_api_key": ""}}).json()
    with SessionLocal() as db:
        assert db.get(ServerSetting, "agent_langsmith_api_key") is None
    assert setting(view, "agent_langsmith_api_key")["overridden"] is False
    assert get_settings().agent_langsmith_api_key == "lsv2-secret-not-shown-000000"


def test_a_saved_key_that_no_longer_decrypts_falls_back_to_the_environment(signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "the-first-secret-key")
    signed_in(admin=True).put(URL, json={"values": {"agent_langsmith_api_key": "lsv2_pt_old-key-0000"}})
    monkeypatch.setattr(get_settings(), "secret_key", "a-rotated-secret-key")
    server_settings.apply()
    assert get_settings().agent_langsmith_api_key == "lsv2-secret-not-shown-000000"


@pytest.mark.parametrize("key", ["short", "has a space in it", "x" * 201])
def test_a_key_that_is_not_one_token_is_refused(signed_in, monkeypatch, key):
    monkeypatch.setattr(get_settings(), "secret_key", "test-secret-key-for-encryption")
    out = signed_in(admin=True).put(URL, json={"values": {"agent_langsmith_api_key": key}})
    assert out.status_code == 422 and key not in out.text
    with SessionLocal() as db:
        assert db.scalars(select(ServerSetting)).all() == []


def test_without_a_secret_key_the_server_refuses_to_store_a_key(signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "")
    out = signed_in(admin=True).put(URL, json={"values": {"agent_langsmith_api_key": "lsv2_pt_cannot-store-0000"}})
    assert out.status_code == 422 and "SECRET_KEY" in out.json()["detail"] and "cannot-store" not in out.text


def test_only_an_admin_saves_a_key(signed_in, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "test-secret-key-for-encryption")
    out = signed_in().put(URL, json={"values": {"agent_langsmith_api_key": "lsv2_pt_not-an-admin-0000"}})
    assert out.status_code == 403
    assert get_settings().agent_langsmith_api_key == "lsv2-secret-not-shown-000000"
