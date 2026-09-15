"""Accounts, sign-in sessions, per-account data isolation and per-account API keys."""
import io
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

import app.main as main
from app import auth
from app.db import AppSession, SessionLocal
from app.db_migrations import run_migrations
from app.models import AppSetting, CandidateProfile, Evidence, Job, KnowledgeAnswer, User, UserSession
from app.security import decrypt_secret
from app.services.user_settings import user_keys
from app.settings import get_settings
from tests.conftest import ensure_test_user

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def an_account_already_exists(anonymous):
    """Accounts created in these tests are never the first one, so they never claim other test data."""
    ensure_test_user()


def new_email(tag="user"):
    # Mixed case on purpose; example.com because email validation rejects reserved names such as .test
    return f"{tag}-{uuid.uuid4().hex[:8]}@Example.com"


def signed_up(email=None):
    client = TestClient(main.app)
    email = email or new_email()
    response = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "confirm_password": PASSWORD})
    assert response.status_code == 201, response.text
    return client, email.lower()


def account_id(email):
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.email == email))


# ───────────── sign-up, sign-in, sessions ─────────────

def test_signup_checks_email_and_matching_passwords():
    client = TestClient(main.app)
    signup = lambda **body: client.post("/api/auth/signup", json=body)  # noqa: E731
    assert signup(email="not-an-email", password=PASSWORD, confirm_password=PASSWORD).status_code == 422
    short = signup(email=new_email(), password="short", confirm_password="short")
    assert short.status_code == 422 and "at least 10 characters" in short.text
    mismatch = signup(email=new_email(), password=PASSWORD, confirm_password="a different password")
    assert mismatch.status_code == 422 and "do not match" in mismatch.text

    email = new_email()
    created = signup(email=email, password=PASSWORD, confirm_password=PASSWORD)
    assert created.status_code == 201
    assert created.json()["user"] == {"id": account_id(email.lower()), "email": email.lower(), "is_admin": False}
    cookie = created.headers["set-cookie"]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=") and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/api/auth/me").json()["user"]["email"] == email.lower()
    with SessionLocal() as db:
        stored = db.scalar(select(User.password_hash).where(User.email == email.lower()))
        assert stored.startswith("scrypt$") and PASSWORD not in stored

    duplicate = TestClient(main.app).post("/api/auth/signup", json={"email": email.upper(), "password": PASSWORD,
                                                                   "confirm_password": PASSWORD})
    assert duplicate.status_code == 409


def test_pages_and_api_require_sign_in():
    client = TestClient(main.app, follow_redirects=False)
    assert client.get("/api/jobs/search").status_code == 401
    assert client.get("/api/settings").status_code == 401
    home = client.get("/")
    assert home.status_code == 303 and home.headers["location"] == "/login"
    assert 'name="password"' in client.get("/login").text
    assert 'name="confirm_password"' in client.get("/signup").text
    assert client.get("/health").status_code == 200 and client.get("/static/app.js").status_code == 200

    signed_in, _ = signed_up()
    assert signed_in.get("/login", follow_redirects=False).status_code == 303
    assert signed_in.get("/").status_code == 200


def test_login_logout_and_errors_that_do_not_reveal_accounts():
    client, email = signed_up()
    token = client.cookies.get(auth.SESSION_COOKIE)
    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert client.get("/api/auth/me").status_code == 401
    # The old token was revoked on the server, not just removed from the browser.
    replay = TestClient(main.app).get("/api/auth/me", headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"})
    assert replay.status_code == 401

    wrong = client.post("/api/auth/login", json={"email": email, "password": "not the password"})
    unknown = client.post("/api/auth/login", json={"email": new_email("nobody"), "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()

    assert client.post("/api/auth/login", json={"email": email.upper(), "password": PASSWORD}).status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == email


def test_repeated_failed_sign_ins_are_throttled():
    client, email = signed_up()
    client.post("/api/auth/logout")
    for _ in range(auth.LOGIN_FAILURE_LIMIT):
        assert client.post("/api/auth/login", json={"email": email, "password": "not the password"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 429


def test_expired_sessions_are_rejected():
    client, email = signed_up()
    with SessionLocal() as db:
        db.execute(update(UserSession).where(UserSession.user_id == account_id(email))
                   .values(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)))
        db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_cross_site_writes_are_blocked():
    client, _ = signed_up()
    blocked = client.post("/api/evidence", json={"claim": "Forged claim"}, headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403
    assert client.post("/api/evidence", json={"claim": "Real claim"}, headers={"Origin": "http://testserver"}).status_code == 200


# ───────────── isolation ─────────────

def test_each_account_only_sees_and_changes_its_own_data():
    alice, _ = signed_up(new_email("alice"))
    bob, _ = signed_up(new_email("bob"))

    posting = {"url": f"https://jobs.example/{uuid.uuid4().hex}", "title": "Staff Engineer", "company": "Acme",
               "description": "Python services"}
    job = alice.post("/api/jobs/from-url", json=posting).json()["job"]
    evidence_id = alice.post("/api/evidence", json={"claim": "Cut cloud costs by 30%", "company": "Acme"}).json()["id"]
    knowledge_id = alice.post("/api/knowledge", json={"question": "Notice period?", "answer": "30 days"}).json()["id"]
    assert alice.put("/api/profile", json={"name": "Alice", "master_resume": "Built Python services at 10k rps"}).status_code == 200
    assert alice.post("/api/profile/resume", files={"file": ("cv.txt", io.BytesIO(b"Alice's resume"), "text/plain")}).status_code == 200
    assert alice.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"}).status_code == 200
    resume_id = alice.post(f"/api/jobs/{job['id']}/tailor").json()["resume_id"]
    assert alice.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    assert alice.post(f"/api/jobs/{job['id']}/applied", json={}).status_code == 200
    application_id = alice.get("/api/tracker").json()["applications"][0]["id"]

    # Bob's workspace is empty ...
    assert bob.get("/api/jobs/search").json()["total"] == 0
    assert bob.get("/api/evidence").json() == [] and bob.get("/api/knowledge").json() == []
    assert bob.get("/api/profile").json()["name"] == "" and bob.get("/api/profile/resumes").json() == []
    assert bob.get("/api/tracker").json()["applications"] == [] and bob.get("/api/applications").json() == []
    assert bob.get("/api/approvals").json() == [] and bob.get("/api/tailor/queue").json() == []
    assert bob.get("/api/profile/resume").status_code == 404
    # ... and he cannot read or change anything of Alice's by ID.
    for path in (f"/api/jobs/{job['id']}", f"/api/tailor/{job['id']}", f"/api/jobs/{job['id']}/apply-kit",
                 f"/api/jobs/{job['id']}/resume.pdf", f"/api/jobs/{job['id']}/application-detail"):
        assert bob.get(path).status_code == 404, path
    assert bob.post(f"/api/jobs/{job['id']}/triage", json={"action": "skip"}).status_code == 404
    assert bob.post(f"/api/jobs/{job['id']}/tailor").status_code == 404
    assert bob.patch(f"/api/evidence/{evidence_id}", json={"verified": False}).status_code == 404
    assert bob.delete(f"/api/knowledge/{knowledge_id}").status_code == 404
    assert bob.patch(f"/api/resumes/{resume_id}/edits/0", json={"status": "accepted"}).status_code == 404
    assert bob.post(f"/api/applications/{application_id}/stage", json={"stage": "screening"}).status_code == 404
    assert bob.post("/api/jobs/bulk-delete", json={"ids": [job["id"]], "include_submitted": True}).json()["deleted"] == 0

    # The same posting and question do not clash across accounts.
    assert bob.post("/api/jobs/from-url", json=posting).json()["created"] is True
    assert bob.post("/api/knowledge", json={"question": "Notice period?", "answer": "60 days"}).status_code == 200

    # Alice's data is intact.
    assert alice.get("/api/jobs/search").json()["total"] == 1
    assert [k["answer"] for k in alice.get("/api/knowledge").json()] == ["30 days"]
    assert alice.get(f"/api/jobs/{job['id']}").status_code == 200
    assert alice.get("/api/profile/resume").content == b"Alice's resume"


def test_first_account_owns_existing_data_and_the_servers_keys(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", connect_args={"check_same_thread": False})
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, class_=AppSession)
    with sessions() as db:  # a single-user install from before accounts existed
        db.add_all([CandidateProfile(name="Legacy owner"), AppSetting(llm_provider="openrouter", model="openrouter/free"),
                    Job(source="manual", source_ref="old", title="Old job", company="Co"), Evidence(claim="Old claim"),
                    KnowledgeAnswer(question="Q", question_key="k", answer="A")])
        db.commit()
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "sk-or-from-env")

    with sessions() as db:
        first = auth.register_user(db, "Owner@Example.com", PASSWORD)
        second = auth.register_user(db, "second@example.com", PASSWORD)
        assert first.is_admin and not second.is_admin and first.email == "owner@example.com"
        for model in (CandidateProfile, AppSetting, Job, Evidence, KnowledgeAnswer):
            assert db.scalar(select(func.count()).select_from(model).where(model.user_id == first.id)) == 1, model
            assert db.scalar(select(func.count()).select_from(model).where(model.user_id.is_(None))) == 0, model
        assert db.scalar(select(CandidateProfile.name).where(CandidateProfile.user_id == first.id)) == "Legacy owner"
        assert user_keys(db, first.id)["openrouter"] == "sk-or-from-env"
        assert user_keys(db, second.id)["openrouter"] is None
        with pytest.raises(auth.AccountExists):
            auth.register_user(db, "OWNER@example.com", PASSWORD)
    engine.dispose()


# ───────────── per-account API keys ─────────────

def test_api_keys_are_encrypted_and_private_to_each_account(monkeypatch):
    alice, alice_email = signed_up()
    bob, _ = signed_up()
    saved = alice.put("/api/settings/keys", json={"openrouter": "sk-or-v1-alice-secret-1234",
                                                  "apify": "apify_api_alice_5678"}).json()
    assert saved["keys"]["openrouter"] == {"configured": True, "hint": "…1234"} and saved["configured"]["apify"] is True
    assert "alice-secret" not in alice.get("/api/settings").text
    with SessionLocal() as db:
        row = db.scalar(select(AppSetting).where(AppSetting.user_id == account_id(alice_email)))
        assert "alice" not in row.openrouter_api_key_enc
        assert decrypt_secret(row.openrouter_api_key_enc) == "sk-or-v1-alice-secret-1234"
    assert bob.get("/api/settings").json()["configured"]["openrouter"] is False

    removed = alice.put("/api/settings/keys", json={"openrouter": ""}).json()
    assert removed["keys"]["openrouter"]["configured"] is False and removed["keys"]["apify"]["configured"] is True

    monkeypatch.setattr(get_settings(), "secret_key", None)
    refused = alice.put("/api/settings/keys", json={"openai": "sk-new-key-0000"})
    assert refused.status_code == 400 and "SECRET_KEY" in refused.json()["detail"]


def test_ai_calls_use_the_signed_in_accounts_own_key(monkeypatch):
    alice, _ = signed_up()
    alice.put("/api/settings/keys", json={"openrouter": "sk-or-v1-alice-key"})
    assert alice.put("/api/settings", json={"llm_provider": "openrouter", "model": "openrouter/free",
                                            "application_mode": "manual_approval", "min_review_score": 70}).status_code == 200
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"score": 77}'))])
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: reply))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    job = alice.post("/api/jobs/from-url", json={"url": f"https://jobs.example/{uuid.uuid4().hex}", "title": "ML Lead",
                                                 "company": "Beta", "description": "Python"}).json()["job"]
    ranked = alice.post(f"/api/jobs/{job['id']}/rank")
    assert ranked.status_code == 200 and ranked.json()["score"] == 77
    assert captured["api_key"] == "sk-or-v1-alice-key"

    bob, _ = signed_up()  # no key saved: a clear error instead of borrowing anyone else's
    assert bob.put("/api/settings", json={"llm_provider": "openrouter", "model": "openrouter/free",
                                          "application_mode": "manual_approval", "min_review_score": 70}).status_code == 200
    bob_job = bob.post("/api/jobs/from-url", json={"url": f"https://jobs.example/{uuid.uuid4().hex}", "title": "ML Lead",
                                                   "company": "Beta", "description": "Python"}).json()["job"]
    refused = bob.post(f"/api/jobs/{bob_job['id']}/rank")
    assert refused.status_code == 400 and "add your OpenRouter API key" in refused.json()["detail"]


def test_scheduler_status_hides_cross_account_details_from_non_admins():
    member, _ = signed_up()
    jobs = member.get("/api/system/scheduler").json()["jobs"]
    assert jobs and all(job["last_result"] is None and job["last_error"] is None for job in jobs)


def test_accounts_get_profiles_on_databases_whose_ids_had_no_sequence(pg_sessions):
    """Pre-migration databases created these tables without an id sequence, so only one row could exist."""
    import importlib

    from sqlalchemy import text

    from app.services.user_settings import profile_for, settings_for
    from tests.conftest import make_user

    repair = importlib.import_module("app.migrations.versions.0005_one_per_user_ids")
    with pg_sessions.begin() as db:
        for table in repair.ONE_PER_USER:
            db.execute(text(f"TRUNCATE {table}"))
            db.execute(text(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT"))
            db.execute(text(f"DROP SEQUENCE IF EXISTS {table}_id_seq"))  # now shaped like the legacy table
    with pg_sessions() as db, pytest.raises(Exception):
        profile_for(db, make_user(pg_sessions))  # the bug: fails instead of silently returning None
    with pg_sessions.begin() as db:
        for table in repair.ONE_PER_USER:
            repair.ensure_id_sequence(db.connection(), table)
            repair.ensure_id_sequence(db.connection(), table)  # running it twice is harmless
    with pg_sessions() as db:
        first, second = make_user(pg_sessions), make_user(pg_sessions)
        assert profile_for(db, first).id != profile_for(db, second).id
        assert settings_for(db, first).id != settings_for(db, second).id
