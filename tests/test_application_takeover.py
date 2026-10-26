"""Human takeover, saved logins and signup on the browser-use backend (Phase 6), on PostgreSQL, with an in-process fake
sidecar (no window opens) that behaves like the runner: a login wall, a signup page, then the form.

  * a login wall with no saved login keeps the window for the person (PENDING, NEEDS_HUMAN, takeover); when they say
    they are done the run re-observes the page and fills it — never replaying anything;
  * a saved login on an allowlisted domain is filled once by Python (signed LOGIN click), and the password never
    reaches a task, an attempt, a checkpoint, a log line or another domain;
  * a signup page gets a generated password shown once, and "Save this login" stores it with the window's session;
  * a takeover nobody comes back to ends NEEDS_HUMAN with the shared blocker;
  * another account cannot continue, sign up or save a login on someone else's execution.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.agents import checkpoint
from app.infrastructure.application_browser import browser_use_sidecar as bus
from app.models import ApplicationExecutionTask, AtsCredential, AtsSession, Job
from app.services import application_execution, ats_auth
from tests import application_support as aps
from tests import qualification_support as qs

URL = "https://acme.wd5.myworkdayjobs.com/External/job/Singapore/Platform-Engineer_R1"
DOMAIN = "acme.wd5.myworkdayjobs.com"
PASSWORD = "Saved-Vault-Pa55!"
LOGIN = [{"key": "u", "tag": "input", "type": "email", "label": "Email address", "in_form": True, "form_index": 0},
         {"key": "p", "tag": "input", "type": "password", "label": "Password", "in_form": True, "form_index": 0},
         {"key": "s", "tag": "button", "type": "submit", "label": "Sign In", "submit_like": True, "in_form": True,
          "form_index": 0}]
SIGNUP = [*LOGIN[:2], {"key": "p2", "tag": "input", "type": "password", "label": "Verify new password"},
          {"key": "s2", "tag": "button", "type": "submit", "label": "Create Account", "submit_like": True}]
FORM = [{"key": "n", "tag": "input", "type": "text", "label": "Full name *", "required": True, "in_form": True},
        {"key": "e", "tag": "input", "type": "email", "label": "Email *", "required": True, "in_form": True},
        {"key": "r", "tag": "input", "type": "file", "label": "Resume *", "required": True, "in_form": True},
        {"key": "q", "tag": "input", "type": "text", "label": aps.PYTHON_QUESTION + " *", "required": True,
         "in_form": True},
        {"key": "go", "tag": "button", "type": "submit", "label": "Submit", "submit_like": True, "in_form": True}]
HTML = {"login": '<form><input type="email"><input type="password"><button>Sign In</button></form><p>Sign in</p>',
        "signup": '<form><input type="password"><input type="password"></form><p>Create an account</p>',
        "form": "<form><input name=n><input type=file></form><p>Apply</p>"}


class FakeSession:
    def __init__(self, manager: FakeManager, user_id: int, task_id: int, storage: dict | None):
        self.manager, self.user_id, self.task_id = manager, user_id, task_id
        self.session_id, self.hmac_key, self.headless = "fake", b"k" * 32, False
        self.parked_until: float | None = None
        self.alive = True
        self.values: dict[str, Any] = {}
        self.commands: list[tuple[str, dict]] = []
        self.storage = storage

    @property
    def page(self) -> str:
        return self.manager.page

    async def call(self, cmd: str, **args: Any) -> dict[str, Any]:
        from app.services import redaction

        self.commands.append((cmd, {k: v for k, v in args.items() if k != "secret"}))
        if cmd == "goto":
            return {"final_url": URL, "status": 200, "title": "Acme careers"}
        if cmd == "observe":
            elements = {"login": LOGIN, "signup": SIGNUP, "form": FORM}[self.page]
            return {"url": URL, "title": "Acme", "elements": [dict(e) for e in elements], "html": HTML[self.page],
                    "text": "Acme careers", "blocked_requests": 0}
        if cmd in ("fill", "select"):
            self.values[args["key"]] = args.get("text", args.get("value"))
            return {"filled": True}
        if cmd == "upload":
            self.values[args["key"]] = [args["path"].rsplit("/", 1)[-1]]
            return {"uploaded": True}
        if cmd == "read":
            value = self.values.get(args["key"])
            out = {"files": value} if isinstance(value, list) else {"value": value or ""}
            return {**out, "filled": bool(value)}
        if cmd == "point":
            self.manager.pointed.append((list(args.get("keys") or []), args.get("message")))
            return {"marked": len(args.get("keys") or [])}
        if cmd == "unpoint":
            return {"cleared": True}
        if cmd == "login_fill":
            self.manager.secrets.append((args["key"], args["secret"], args["domain"],
                                         redaction.redact_text(args["secret"])))
            return {"filled": True}
        if cmd == "login_submit":
            body, signature = args["token"].rsplit(".", 1)
            import base64
            import hashlib
            import hmac
            assert hmac.compare_digest(hmac.new(self.hmac_key, body.encode(), hashlib.sha256).hexdigest(), signature)
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            assert claims["kind"] == "LOGIN" and claims["domain"] == DOMAIN and claims["key"] == "s"
            if [s for _, s, _, _ in self.manager.secrets][-1] == PASSWORD:
                self.manager.page = "form"
            return {"clicked": True}
        if cmd == "set_takeover":
            return {"takeover": args.get("on")}
        if cmd == "storage_state":
            return {"state": {"cookies": [{"name": "wd-session", "value": "abc", "domain": DOMAIN},
                                          {"name": "tracker", "value": "x", "domain": ".ads.example"}],
                              "origins": []}}
        if cmd in ("close", "health"):
            return {}
        raise AssertionError(f"unexpected command {cmd}")

    async def close(self) -> None:
        self.alive = False


class FakeManager:
    def __init__(self, page: str = "login"):
        self.page = page
        self.sessions: dict[tuple[int, int], FakeSession] = {}
        self.started: list[dict[str, Any]] = []
        self.secrets: list[tuple] = []
        self.pointed: list[tuple] = []
        self.parked: list[bool] = []  # whether each kept window may send forms (writes)
        self.loop = None

    def available(self) -> bool:
        return True

    async def start(self, *, user_id, task_id, job_id, allowed_hosts, upload_dir, storage_state=None,
                    headless=None, allow_private=False):
        self.loop = asyncio.get_running_loop()
        self.started.append({"user": user_id, "task": task_id, "hosts": allowed_hosts, "storage": storage_state})
        session = FakeSession(self, user_id, task_id, storage_state)
        self.sessions[(user_id, task_id)] = session
        return session

    def get(self, user_id, task_id):
        session = self.sessions.get((user_id, task_id))
        return session if session is not None and session.alive else None

    async def park(self, session, minutes):
        session.parked_until = time.monotonic() + minutes * 60
        self.parked.append(True)  # handed to the person: the window works like a normal browser
        return True

    async def hold(self, session, minutes):
        session.parked_until = time.monotonic() + minutes * 60

    async def unpark(self, session):
        session.parked_until = None

    async def close(self, user_id, task_id):
        session = self.sessions.pop((user_id, task_id), None)
        if session is not None:
            await session.close()

    async def reap(self):
        return 0

    def run_threadsafe(self, coro, timeout=60):
        return asyncio.run(coro)


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch, tmp_path):
    aps.configure(monkeypatch, "assist")
    settings = aps.get_settings()
    for name, value in (("mcp_packet_dir", str(tmp_path / "packets")), ("browser_use_webui_enabled", True),
                        ("browser_use_headless", False), ("secret_key", "test-secret-for-the-vault"),
                        ("application_login_domains", DOMAIN), ("application_auth_sessions_enabled", True)):
        monkeypatch.setattr(settings, name, value)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    yield qs.app_sessions(pg_engine)
    bus.set_sidecars(None)


def account(sessions) -> tuple[int, int]:
    user = aps.owner(sessions)
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    return user, job_id


def drive(sessions) -> None:
    asyncio.run(aps.drive_async(sessions, passes=6))


def client_as(user_id: int, monkeypatch, sessions) -> TestClient:
    """The API as `user_id`, on the test database's sessions (the account filter applies as in production)."""
    from app import auth
    from app.auth import CurrentUser, get_user_db
    from app.main import app

    def user_db():
        db = sessions()
        db.info["user_id"] = user_id
        try:
            yield db
        finally:
            db.close()
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, "x@jobhunt.test", False))
    monkeypatch.setitem(app.dependency_overrides, get_user_db, user_db)
    return TestClient(app)


def test_a_login_wall_waits_in_the_window_and_the_run_resumes_from_a_fresh_look(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_HUMAN")
    assert task.interrupt["takeover"] and task.interrupt["reason"] == "LOGIN_REQUIRED"
    assert task.interrupt["login_domain"] == DOMAIN
    assert fake.get(user, task.id).parked_until is not None  # the window is kept for the person
    assert fake.parked == [True]  # handed to the person, who signs in there
    drive(sessions)
    assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"  # nothing happens until they say so

    fake.page = "form"  # the person signs in, in the window
    before = len(fake.sessions[(user, task.id)].commands)
    response = client_as(user, monkeypatch, sessions).post(f"/api/application-executions/{task.id}/continue")
    assert response.status_code == 200, response.text
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT")
    resumed = [c for c, _ in fake.sessions[(user, task.id)].commands][before:]
    assert resumed[:2] == ["goto", "observe"]  # re-observed from the start of the page, nothing replayed
    assert "login_fill" not in resumed and "login_submit" not in resumed
    assert len(fake.started) == 1  # the same window, not a new browser

    saved = client_as(user, monkeypatch, sessions).post(f"/api/application-executions/{task.id}/save-login",
                                              json={"password": "Typed-By-Person-1"})
    assert saved.status_code == 200 and saved.json()["session_saved"], saved.text
    assert "Typed-By-Person-1" not in saved.text
    with sessions() as db:
        kept = ats_auth.session_state(db, user, DOMAIN)
        assert [c["name"] for c in kept["cookies"]] == ["wd-session"]  # nothing of another site
        assert ats_auth.login_secret(db, user, DOMAIN).password == "Typed-By-Person-1"


def test_a_saved_login_is_filled_once_on_its_own_domain_and_never_leaks(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        ats_auth.save_credential(db, user, DOMAIN, "asha@example.test", PASSWORD)
        db.commit()
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.outcome == "READY_TO_SUBMIT"
    fills = [(key, domain, shown) for key, _, domain, shown in fake.secrets]
    assert fills == [("u", DOMAIN, "[redacted]"), ("p", DOMAIN, "[redacted]")]  # redacted from logs while in use
    with sessions() as db:
        for table in ("application_execution_tasks", "application_execution_attempts", "agent_threads",
                      "agent_effects", "system_logs", "external_api_request_logs", "checkpoint_blobs",
                      "checkpoint_writes"):
            dump = " ".join(str(row) for row in db.execute(text(f"SELECT * FROM {table}")).all())
            assert PASSWORD not in dump, table


def test_a_failed_saved_login_waits_for_the_person_and_tries_only_once(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        ats_auth.save_credential(db, user, DOMAIN, "asha@example.test", "an-outdated-password")
        db.commit()
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.stage, task.interrupt["reason"]) == ("NEEDS_HUMAN", "LOGIN_FAILED")
    assert [cmd for cmd, _ in fake.sessions[(user, task.id)].commands].count("login_submit") == 1


def test_a_signup_gets_a_generated_password_once_and_save_login_keeps_it(sessions, monkeypatch):
    fake = FakeManager("signup")
    bus.set_sidecars(fake)
    user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.interrupt["reason"] == "ACCOUNT_REQUIRED" and task.interrupt["takeover"]
    client = client_as(user, monkeypatch, sessions)
    issued = client.post(f"/api/application-executions/{task.id}/signup").json()
    assert issued["username"] == "asha@example.test" and len(issued["password"]) == 24
    with sessions() as db:
        row = db.scalar(select(AtsCredential))
        assert row.status == "PENDING" and issued["password"] not in row.password_encrypted
    assert client.post(f"/api/application-executions/{task.id}/save-login", json={}).status_code == 200
    listing = client.get("/api/settings/ats-credentials")
    assert issued["password"] not in listing.text and listing.json()["credentials"][0]["status"] == "ACTIVE"
    with sessions() as db:
        assert ats_auth.login_secret(db, user, DOMAIN).password == issued["password"]
        assert db.scalar(select(AtsSession)).domain == DOMAIN


def test_a_takeover_nobody_comes_back_to_ends_with_the_blocker(sessions):
    from datetime import timedelta

    bus.set_sidecars(FakeManager("login"))
    _user, job_id = account(sessions)
    drive(sessions)
    with sessions() as db:
        assert application_execution.end_expired_takeovers(db, aps.utcnow() + timedelta(minutes=16)) == 1
        task = db.scalar(select(ApplicationExecutionTask))
        assert (task.status, task.outcome, task.result_code) == ("COMPLETED", "NEEDS_HUMAN", "TAKEOVER_EXPIRED")
        job = db.get(Job, job_id)
        assert job.application.apply_blocker["reason"] == "login_required"


def test_another_account_cannot_touch_someone_elses_window(sessions, monkeypatch):
    fake = FakeManager("login")
    bus.set_sidecars(fake)
    _user, job_id = account(sessions)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    stranger = aps.owner(sessions)
    client = client_as(stranger, monkeypatch, sessions)
    for path in ("continue", "signup", "save-login"):
        response = client.post(f"/api/application-executions/{task.id}/{path}", json={"password": "x" * 10})
        assert response.status_code in (404, 409), path
    assert fake.get(stranger, task.id) is None
    assert aps.task_of(sessions, job_id).stage == "NEEDS_HUMAN"
    with sessions() as db:
        assert db.scalar(select(AtsCredential)) is None



def test_a_field_nothing_verified_fills_is_pointed_out_in_the_open_window_and_kept(sessions, monkeypatch):
    """The Python question has no verified answer: the run fills the rest, points to that field in the window and
    waits (FIELDS_NEED_YOU, its label listed). The person types in the window and continues: the page is not reloaded,
    the field is accepted as theirs (PERSON), the run ends READY_TO_SUBMIT, and what they typed can be saved."""
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)  # no saved answer to the Python question
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.interrupt["reason"]) == ("PENDING", "NEEDS_HUMAN", "FIELDS_NEED_YOU")
    assert [f["label"] for f in task.interrupt["fields"]] == [aps.PYTHON_QUESTION]
    assert task.interrupt["question_ids"] and fake.pointed[-1][0] == ["q"]
    assert "fill the 1 highlighted field" in fake.pointed[-1][1]
    assert fake.parked == [True]  # handed to the person to fill the fields (and submit, if they choose)
    client = client_as(user, monkeypatch, sessions)
    kit = client.get(f"/api/jobs/{job_id}/apply-kit").json()
    assert kit["execution_takeover"]["fields"][0]["label"] == aps.PYTHON_QUESTION
    assert [q["question"] for q in kit["execution_questions"]] == [aps.PYTHON_QUESTION]  # answer here or in the page
    session = fake.sessions[(user, task.id)]
    before = [c for c, _ in session.commands].count("goto")
    session.values["q"] = "11"  # the person types it in the window
    assert client.post(f"/api/application-executions/{task.id}/continue").status_code == 200
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT")
    assert [c for c, _ in session.commands].count("goto") == before  # picked up in place, never reloaded
    # The filled form stays open for the person to submit, like a normal browser: nothing to start first.
    assert fake.parked == [True, True] and fake.get(user, task.id).parked_until is not None
    field_id = next(f["id"] for f in task.form["fields"] if f["label"] == aps.PYTHON_QUESTION)
    assert task.plan["fields"][field_id]["status"] == "PERSON" and "11" not in str(task.plan)
    stranger = aps.owner(sessions)
    assert client_as(stranger, monkeypatch, sessions).post(
        f"/api/application-executions/{task.id}/save-window-answers").status_code == 404
    saved = client_as(user, monkeypatch, sessions).post(f"/api/application-executions/{task.id}/save-window-answers")
    assert saved.json() == {"saved": 1} and "11" not in saved.text
    with sessions() as db:
        from app.models import KnowledgeAnswer
        row = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question == aps.PYTHON_QUESTION))
        assert (row.answer, row.verified) == ("11", True)


def test_without_a_visible_window_the_questions_go_to_the_apply_kit(sessions, monkeypatch):
    """Headless (or remote) installs cannot hand a page to a person: an unanswered question stops the run before the
    browser fills (NEEDS_INPUT, answered in the Apply kit), and nothing is pointed at."""
    monkeypatch.setattr(aps.get_settings(), "browser_use_headless", True)
    fake = FakeManager("form")
    bus.set_sidecars(fake)
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, URL)
    aps.enqueue(sessions, user, job_id)
    drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_INPUT") and fake.pointed == []
