"""The browser-use backend with the real runner (sidecars/browser_use_runner), started with the browser-use Web UI's own
Python. Skipped unless that Python exists (BROWSER_USE_PYTHON, default the local web-ui conda env); the end-to-end run
also needs PostgreSQL. Everything is served by a local fake site; nothing reaches the internet or is submitted.

  * the runner reports browser-use 0.1.48 and protocol 1, and its environment carries none of Job Hunt OS's secrets;
  * it reads a form as indexed elements, fills only what it is told, uploads only the run's own files, never fills a
    password field, refuses a submit-like click, a forged token and an address off the allowed hosts, and aborts every
    non-GET request until a signed SUBMIT arms one domain;
  * a timeout or a dead process is SidecarUnavailable (the process is killed), and a new session starts cleanly;
  * one account never reaches another's session;
  * a dynamic form (hidden behind "Apply now", with instructions to the agent in the page) goes through the real
    scheduler pass: the navigator (mock model, through the gateway) reveals it, Python fills it, it ends
    READY_TO_SUBMIT with zero POSTs.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents import checkpoint
from app.infrastructure.application_browser import browser_use_sidecar as bus
from app.models import ApplicationExecutionTask, LLMRequestLog
from app.services import browser_actions
from app.services.ats_forms import generic
from tests import application_support as aps
from tests import guarded_support as gs
from tests import qualification_support as qs

RUNNER = Path(__file__).resolve().parents[1] / "sidecars/browser_use_runner/runner.py"
PYTHON = os.environ.get("BROWSER_USE_PYTHON") or "/Users/rohanroy/miniconda3/envs/web/bin/python"
needs_runner = pytest.mark.skipif(not Path(PYTHON).exists(), reason="no browser-use Python (BROWSER_USE_PYTHON)")
pytestmark = needs_runner


@pytest.fixture
def runner(monkeypatch):
    settings = aps.get_settings()
    for name, value in (("browser_use_webui_enabled", True), ("browser_use_python", PYTHON),
                        ("browser_use_headless", True), ("browser_use_command_timeout_seconds", 60),
                        ("application_allow_private_hosts", True)):
        monkeypatch.setattr(settings, name, value)
    bus.set_sidecars(None)
    yield
    bus.set_sidecars(None)


def test_the_runner_is_browser_use_0_1_48_and_gets_no_secrets(runner, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-for-the-runner-000000")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret@db/prod")
    assert set(bus._environment()) <= {"PATH", "HOME", "ANONYMIZED_TELEMETRY", "PYTHONUNBUFFERED",
                                       "PYTHONNOUSERSITE", "PLAYWRIGHT_BROWSERS_PATH"}
    info = asyncio.run(bus.health())
    assert info["compatible"] and info["browser_use"] == "0.1.48" and info["protocol"] == 1


def test_the_runner_reads_fills_and_refuses_everything_it_must(runner, tmp_path):
    from app.services import application_execution

    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 test")
    outside = tmp_path.parent / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4 other")
    manager = bus.get_sidecars()
    with gs.FakeATS("confirm") as ats, application_execution.background_loop() as loop:
        run = application_execution.loop_runner(loop, timeout=90)
        session = run(manager.start(user_id=1, task_id=10, job_id=5, allowed_hosts=["127.0.0.1"],
                                    upload_dir=str(tmp_path), allow_private=True))
        try:
            assert manager.get(2, 10) is None and manager.get(1, 11) is None  # nobody else's key
            run(session.call("goto", url=ats.url()))
            observation = run(session.call("observe", html=True))
            form = generic.analyze(observation["url"], observation["elements"])
            by_label = {f.label.lower(): f for f in form.fields}
            values = {by_label["first name"].id: "Asha", by_label["last name"].id: "Rao",
                      by_label["email"].id: "asha@example.test",
                      by_label["resume/cv"].id: {"path": str(tmp_path / "resume.pdf")}}
            plan = {"fields": {fid: {"status": "RESOLVED"} for fid in values}}
            result = browser_actions.FillResult()
            browser_actions.fill_deterministic(session, run, form, plan, values, observation, result, 60)
            refusals = {}
            submit = next(e for e in observation["elements"] if e["submit_like"])
            for name, command, args in (("click_submit", "click", {"key": submit["key"]}),
                                        ("forged", "submit", {"key": submit["key"], "token": "e30.deadbeef"}),
                                        ("elsewhere", "goto", {"url": "http://example.com/"}),
                                        ("upload", "upload", {"key": by_label["resume/cv"].locator[4:],
                                                              "path": str(outside)})):
                try:
                    run(session.call(command, **args))
                    refusals[name] = None
                except bus.SidecarRefused as exc:
                    refusals[name] = exc.code
            after = run(session.call("observe"))
            reads = {f.label: run(session.call("read", key=f.locator[4:])) for f in form.fields if f.id in values}
            missing = browser_actions.read_back(session, run, form, values, after)
        finally:
            run(manager.close(1, 10))
        posts = list(ats.posts)
    assert form.adapter == "browser_use" and len(form.fields) >= 9
    assert {"first_name", "last_name", "email", "resume"} <= {f.role for f in form.fields}
    assert len(result.filled) == 4 and not result.refused
    assert reads["Email"]["value"] == "asha@example.test" and reads["Resume/CV"]["files"] == ["resume.pdf"]
    assert [m for m in missing if m in values] == []
    assert refusals == {"click_submit": "SUBMIT_REFUSED", "forged": "BAD_TOKEN", "elsewhere": "URL_NOT_ALLOWED",
                        "upload": "FILE_NOT_ALLOWED"}
    assert posts == [] and after["blocked_requests"] >= 1  # the page's beacon was aborted


def test_only_a_signed_submit_arms_one_domain_and_posts(runner, tmp_path):
    async def go(url):
        manager = bus.get_sidecars()
        session = await manager.start(user_id=1, task_id=20, job_id=5, allowed_hosts=["127.0.0.1"],
                                      upload_dir=str(tmp_path), allow_private=True)
        try:
            await session.call("goto", url=url)
            observation = await session.call("observe")
            submit = next(e for e in observation["elements"] if e["submit_like"])
            wrong = bus.sign_action(session, "SUBMIT", submit["key"], "other.example")
            try:
                await session.call("submit", key=submit["key"], token=wrong)
                wrong_domain = None
            except bus.SidecarRefused as exc:
                wrong_domain = exc.code
            token = bus.sign_action(session, "SUBMIT", submit["key"], "127.0.0.1")
            await session.call("submit", key=submit["key"], token=token)
            evidence = await session.call("evidence")
            return wrong_domain, evidence
        finally:
            await manager.close(1, 20)

    with gs.FakeATS("confirm") as ats:
        wrong_domain, evidence = asyncio.run(go(ats.url()))
        submits = list(ats.submits)
    assert wrong_domain == "BAD_TOKEN"
    assert submits == ["/apply/submit"]  # the signed click, and only it, reached the site
    assert evidence["url"].endswith("/confirmation.html") and evidence["form_inputs"] == 0
    assert any(r["method"] == "POST" and r["host"] == "127.0.0.1" for r in evidence["responses"])


def test_a_silent_runner_times_out_and_is_killed(runner, monkeypatch, tmp_path):
    silent = tmp_path / "silent.py"
    silent.write_text("import time\ntime.sleep(600)\n")
    fake_python = tmp_path / "python"
    fake_python.write_text(f"#!/bin/sh\nexec {sys.executable} {silent}\n")
    fake_python.chmod(0o755)
    monkeypatch.setattr(aps.get_settings(), "browser_use_python", str(fake_python))

    async def go():
        manager = bus.get_sidecars()
        process = await asyncio.create_subprocess_exec(str(fake_python), stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE)
        session = bus.SidecarSession(user_id=1, task_id=1, job_id=None, process=process)
        with pytest.raises(bus.SidecarUnavailable):
            await session.call("health", timeout=1, record=False)
        assert not session.alive  # killed, not left running
        with pytest.raises(bus.SidecarUnavailable):
            await manager.start(user_id=1, task_id=2, job_id=None, allowed_hosts=[], upload_dir=str(tmp_path))
    monkeypatch.setattr(aps.get_settings(), "browser_use_command_timeout_seconds", 1)
    original = bus.SidecarSession.call

    async def quick(self, cmd, *, timeout=None, record=True, **args):
        return await original(self, cmd, timeout=min(timeout or 1, 2), record=record, **args)
    monkeypatch.setattr(bus.SidecarSession, "call", quick)
    asyncio.run(go())


def test_a_dead_runner_is_unavailable_and_a_new_session_starts(runner, tmp_path):
    async def go():
        manager = bus.get_sidecars()
        first = await manager.start(user_id=1, task_id=30, job_id=None, allowed_hosts=["127.0.0.1"],
                                    upload_dir=str(tmp_path), allow_private=True)
        first.process.kill()
        await first.process.wait()
        with pytest.raises(bus.SidecarUnavailable):
            await first.call("observe")
        assert manager.get(1, 30) is None
        second = await manager.start(user_id=1, task_id=30, job_id=None, allowed_hosts=["127.0.0.1"],
                                     upload_dir=str(tmp_path), allow_private=True)
        info = await second.call("health")
        await manager.close(1, 30)
        return info
    assert asyncio.run(go())["protocol"] == 1


def test_a_stray_line_on_the_pipe_never_breaks_a_session(runner, tmp_path):
    """browser-use logs to stdout ("❌  Failed to update state: …") and some of its helpers print(). The runner keeps
    stdout for replies only; the client also skips a line that is not a reply instead of failing the session."""
    chatty = tmp_path / "chatty.py"
    chatty.write_text("import json, sys\n"
                      "line = json.loads(sys.stdin.readline())\n"
                      "print('\\u274c  Failed to update state: Timeout 30000ms exceeded.', flush=True)\n"
                      "print('', flush=True)\n"
                      "print(json.dumps({'id': line['id'], 'ok': True, 'result': {'protocol': 1}}), flush=True)\n")

    async def client():
        process = await asyncio.create_subprocess_exec(sys.executable, str(chatty), stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE)
        session = bus.SidecarSession(user_id=1, task_id=1, job_id=None, process=process)
        return await session.call("health", timeout=10, record=False)
    assert asyncio.run(client()) == {"protocol": 1}

    # The runner itself: a library's print() and log lines go to stderr, the reply alone to stdout.
    probe = tmp_path / "probe.py"
    probe.write_text(f"import asyncio, logging, runpy, sys\n"
                     f"runner = runpy.run_path({str(RUNNER)!r})\n"
                     f"class Noisy(runner['Runner']):\n"
                     f"    async def health(self, args):\n"
                     f"        print('from a helper')\n"
                     f"        logging.getLogger('browser_use').addHandler(logging.StreamHandler(sys.stdout))\n"
                     f"        logging.getLogger('browser_use').error('a library log line')\n"
                     f"        return {{'protocol': 1}}\n"
                     f"runner['main'].__globals__['Runner'] = Noisy\n"
                     f"asyncio.run(runner['main']())\n")
    done = subprocess.run([PYTHON, str(probe)], input='{"id": 1, "cmd": "health", "args": {}}\n', text=True,
                          capture_output=True, timeout=60, check=False)
    assert done.stdout.splitlines() == ['{"id": 1, "ok": true, "result": {"protocol": 1}}']
    assert "from a helper" in done.stderr and "a library log line" in done.stderr


# ───────────── the whole browser-use path through the scheduler ─────────────

def test_a_dynamic_form_is_revealed_and_filled_through_the_scheduler_with_zero_posts(runner, pg_sessions, pg_engine,
                                                                                    monkeypatch, tmp_path):
    aps.configure(monkeypatch, "assist")
    monkeypatch.setattr(aps.get_settings(), "browser_use_webui_enabled", True)  # configure() turns it off
    monkeypatch.setattr(aps.get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    sessions = qs.app_sessions(pg_engine)
    with gs.FakeATS("confirm") as ats:
        user = aps.owner(sessions)
        aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
        job_id = aps.approved_job(sessions, user, ats.url("dynamic_form"))
        aps.enqueue(sessions, user, job_id)
        from app.services import llm_telemetry

        llm_telemetry.use_sessions(sessions)
        try:
            asyncio.run(aps.drive_async(sessions, passes=6))
        finally:
            llm_telemetry.use_sessions(None)
        posts = list(ats.posts)
    with sessions() as db:
        task = db.scalar(select(ApplicationExecutionTask).where(ApplicationExecutionTask.job_id == job_id))
        assert (task.backend, task.status, task.stage, task.outcome) == ("browser_use_webui", "COMPLETED",
                                                                         "READY_TO_SUBMIT", "READY_TO_SUBMIT"), \
            (task.result_code, task.last_error)
        assert task.route == "browser_use"
        fields = {f["label"]: f for f in task.form["fields"]}
        assert fields["Email address"]["role"] == "email" and fields["Resume / CV"]["role"] == "resume"
        plan = task.plan["fields"]
        assert all(plan[f["id"]]["status"] == "RESOLVED" for f in task.form["fields"] if f["required"])
        steps = db.scalars(select(LLMRequestLog).where(LLMRequestLog.prompt_name == "browser_navigation")).all()
        assert steps and all(s.stage == "BROWSER_NAVIGATION" for s in steps)  # the reveal went through the gateway
    assert posts == []  # the page's beacon and its instructions to "click Submit" changed nothing



BOUNDARY_PAGE = b"""<html><body>
<p id="ls"></p>
<script>document.getElementById('ls').textContent = 'stored:' + (localStorage.getItem('jh') || 'none');</script>
<form id="app" method="get" action="/apply/next">
  <label for="q">Your answer</label><input id="q" name="q">
  <button>Continue</button>
</form>
<form id="login" method="post" action="/login">
  <label for="u">Email</label><input id="u" name="u" type="email" autocomplete="username">
  <label for="p">Password</label><input id="p" name="p" type="password">
  <button type="submit">Sign in</button>
</form>
<a href="#" id="ping" onclick="fetch('/ping', {method: 'POST'}); return false">Ping the site</a>
<div id="cv-box"><label for="cv">Resume/CV</label><input id="cv" type="file"></div>
<script>
  // Like the newer Greenhouse board: the chosen file is sent at once (XHR, multipart) and the input is replaced by
  // the file's name.
  document.getElementById('cv').addEventListener('change', function (e) {
    var file = e.target.files[0], body = new FormData();
    body.append('file', file);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.onload = function () { document.getElementById('cv-box').innerHTML = '<p class="file-upload__filename">' + file.name + '</p>'; };
    xhr.send(body);
  });
</script>
</body></html>"""


def test_the_runner_keeps_its_boundaries_on_buttons_logins_storage_and_the_kept_window(runner, tmp_path):
    """* a <button> with no type inside a form submits it: it is submit-like and an unsigned click is refused;
    * a LOGIN signature works only on the control of the form that holds the password field, and the login parts are
      taken from that form, never the application's;
    * a saved session's local storage is restored, not only its cookies;
    * an upload the page starts when a file is chosen goes out, and the runner remembers the file once the page took
      it, even after the page replaced the input with the file's name;
    * while Job Hunt OS drives, the page sends nothing else; a window handed to the person sends what they send."""
    import time

    from app.infrastructure.application_browser.browser_use_sidecar import sign_action
    from app.services import application_backends, application_execution

    manager = bus.get_sidecars()
    with gs.FakeATS("confirm", pages={"boundary.html": BOUNDARY_PAGE}) as ats, \
            application_execution.background_loop() as loop:
        run = application_execution.loop_runner(loop, timeout=90)
        origin = ats.url("boundary").rsplit("/", 1)[0]
        session = run(manager.start(user_id=1, task_id=60, job_id=5, allowed_hosts=["127.0.0.1"],
                                    upload_dir=str(tmp_path), allow_private=True,
                                    storage_state={"cookies": [], "origins": [
                                        {"origin": origin, "localStorage": [{"name": "jh", "value": "signed-in"}]}]}))
        outcomes = {}
        try:
            run(session.call("goto", url=ats.url("boundary")))
            seen = run(session.call("observe"))
            by_label = {e["label"]: e for e in seen["elements"]}
            proceed, sign_in, ping = by_label["Continue"], by_label["Sign in"], by_label["Ping the site"]
            username, password, login_button = application_backends._login_parts(seen)
            for name, command, args in (
                    ("unsigned_continue", "click", {"key": proceed["key"]}),
                    ("login_token_on_continue", "login_submit",
                     {"key": proceed["key"], "token": sign_action(session, "LOGIN", proceed["key"], "127.0.0.1")})):
                try:
                    run(session.call(command, **args))
                    outcomes[name] = None
                except bus.SidecarRefused as exc:
                    outcomes[name] = exc.code
            (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 test")
            uploaded = run(session.call("upload", key=by_label["Resume/CV"]["key"], path=str(tmp_path / "resume.pdf")))
            after_upload = run(session.call("observe"))
            attached = run(session.call("read", key=by_label["Resume/CV"]["key"]))
            run(session.call("click", key=ping["key"]))  # Job Hunt OS drives: nothing else goes out
            time.sleep(0.5)
            while_driving = list(ats.posts)
            session.parked_until = time.monotonic() + 60  # handed to the person (park; headless here)
            run(session.call("set_takeover", on=True, writes=True, seconds=60))
            run(session.call("click", key=ping["key"]))
            time.sleep(0.5)
            handed_over = list(ats.posts)
        finally:
            run(manager.close(1, 60))
    assert "stored:signed-in" in seen["text"]
    assert proceed["type"] == "submit" and proceed["submit_like"]
    assert outcomes == {"unsigned_continue": "SUBMIT_REFUSED", "login_token_on_continue": "NOT_A_LOGIN"}
    assert login_button["key"] == sign_in["key"] and username["label"] == "Email" and password["type"] == "password"
    assert uploaded["uploaded"] and uploaded["upload_requests"] == 1
    assert "Resume/CV" not in {e["label"] for e in after_upload["elements"]}  # the input became the file's name
    assert after_upload["attached"] == {by_label["Resume/CV"]["key"]: "resume.pdf"}
    assert attached["files"] == ["resume.pdf"] and attached["filled"]
    assert while_driving == ["/upload"] and handed_over == ["/upload", "/ping"]


def test_choose_sets_a_dropdown_to_the_exact_option_only(runner, tmp_path):
    from app.services import application_execution

    manager = bus.get_sidecars()
    with gs.FakeATS("confirm") as ats, application_execution.background_loop() as loop:
        run = application_execution.loop_runner(loop, timeout=90)
        session = run(manager.start(user_id=1, task_id=40, job_id=5, allowed_hosts=["127.0.0.1"],
                                    upload_dir=str(tmp_path), allow_private=True))
        try:
            run(session.call("goto", url=ats.url("greenhouse_board_form")))
            observation = run(session.call("observe", html=True))
            form = generic.analyze(observation["url"], observation["elements"])
            combo = next(e for e in observation["elements"] if e.get("role") == "combobox")
            refused = []
            for text in ("Maybe", "Ye"):  # no option, and an option that only contains the answer
                try:
                    run(session.call("choose", key=combo["key"], text=text))
                except bus.SidecarRefused as exc:
                    refused.append(exc.code)
            chosen = run(session.call("choose", key=combo["key"], text=" yes "))
            read = run(session.call("read", key=combo["key"]))
        finally:
            run(manager.close(1, 40))
        posts = list(ats.posts)
    kinds = {f.label: f.kind for f in form.fields}
    assert kinds["Are you open to relocation for this role?"] == "combobox"
    assert not any(f.label in ("", "field") or f.id.startswith("bu_") and not f.label for f in form.fields)
    assert refused == ["NO_SUCH_OPTION", "NO_SUCH_OPTION"] and chosen["value"] == "Yes" and read["value"] == "Yes"
    assert posts == []


def test_a_board_with_dropdowns_goes_to_browser_use_and_ends_ready_to_submit(runner, pg_sessions, pg_engine,
                                                                             monkeypatch, tmp_path):
    """The Playwright read finds a required dropdown it cannot open, hands the run to the browser-use backend, which
    fills every field from verified answers (the dropdown with `choose`) and stops at READY_TO_SUBMIT. Zero POSTs."""
    from app.infrastructure.application_browser import set_application_sessions
    from app.infrastructure.application_browser.ephemeral import EphemeralApplicationSessions
    from app.infrastructure.browser import playwright_service as pw

    aps.configure(monkeypatch, "assist")
    monkeypatch.setattr(aps.get_settings(), "browser_use_webui_enabled", True)  # configure() turns it off
    monkeypatch.setattr(aps.get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))
    gs.route_greenhouse_here(monkeypatch)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    sessions = qs.app_sessions(pg_engine)
    browser = pw.PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False,
                                          navigation_timeout_seconds=20)
    set_application_sessions(EphemeralApplicationSessions(browser, record_telemetry=False))
    try:
        with gs.FakeATS("confirm") as ats:
            user = aps.owner(sessions)
            aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
            aps.answer(sessions, user, "Are you open to relocation for this role?", "Yes")
            job_id = aps.approved_job(sessions, user, ats.url("greenhouse_board_form"))
            aps.enqueue(sessions, user, job_id)

            async def go():
                try:
                    await aps.drive_async(sessions, passes=6)
                finally:
                    await browser.stop()
            asyncio.run(go())
            posts = list(ats.posts)
    finally:
        set_application_sessions(None)
    task = aps.task_of(sessions, job_id)
    assert (task.backend, task.status, task.stage, task.outcome) == ("browser_use_webui", "COMPLETED",
                                                                     "READY_TO_SUBMIT", "READY_TO_SUBMIT"), \
        (task.result_code, task.last_error)
    assert any(f["kind"] == "combobox" for f in task.form["fields"])
    assert posts == []


def test_point_marks_the_fields_and_shows_a_banner_and_unpoint_clears_them(runner, tmp_path):
    from app.services import application_execution

    manager = bus.get_sidecars()
    with gs.FakeATS("confirm") as ats, application_execution.background_loop() as loop:
        run = application_execution.loop_runner(loop, timeout=90)
        session = run(manager.start(user_id=1, task_id=50, job_id=5, allowed_hosts=["127.0.0.1"],
                                    upload_dir=str(tmp_path), allow_private=True))
        try:
            run(session.call("goto", url=ats.url()))
            observation = run(session.call("observe"))
            email = next(e for e in observation["elements"] if e["type"] == "email")
            marked = run(session.call("point", keys=[email["key"], "no-such-key"], note="Needs you",
                                      message="Job Hunt OS needs you: fill the 1 highlighted field"))
            shown = run(session.call("observe", html=True))
            run(session.call("unpoint"))
            cleared = run(session.call("observe", html=True))
            read = run(session.call("read", key=email["key"]))
        finally:
            run(manager.close(1, 50))
        posts = list(ats.posts)
    assert marked == {"marked": 1}
    assert "fill the 1 highlighted field" in shown["text"] and "data-jobhunt-needs-you" in shown["html"]
    assert "highlighted field" not in cleared["text"] and "data-jobhunt-needs-you" not in cleared["html"]
    assert read["filled"] is False and posts == []


def test_the_window_points_at_an_unanswered_field_and_resumes_in_place(runner, pg_sessions, pg_engine, monkeypatch,
                                                                       tmp_path):
    from app.services import application_execution

    aps.configure(monkeypatch, "assist")
    for name, value in (("browser_use_webui_enabled", True), ("browser_use_headless", False),
                        ("mcp_packet_dir", str(tmp_path / "packets"))):
        monkeypatch.setattr(aps.get_settings(), name, value)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    sessions = qs.app_sessions(pg_engine)
    manager = _VisibleWindow()
    bus.set_sidecars(manager)
    with gs.FakeATS("confirm") as ats:
        user = aps.owner(sessions)  # no saved answer to the Python question
        job_id = aps.approved_job(sessions, user, ats.url("dynamic_form"))
        aps.enqueue(sessions, user, job_id)

        async def go():
            await aps.drive_async(sessions, passes=6)
            task = aps.task_of(sessions, job_id)
            assert (task.stage, task.interrupt["reason"]) == ("NEEDS_HUMAN", "FIELDS_NEED_YOU"), task.result_code
            assert [f["label"] for f in task.interrupt["fields"]] == [aps.PYTHON_QUESTION]
            session = manager.get(user, task.id)
            page = await session.call("observe", html=True)
            assert "fill the 1 highlighted field" in page["text"]
            loads = [p for m, p in ats.requests if m == "GET" and "dynamic_form" in p]
            field = next(e for e in page["elements"]
                         if e.get("tag") == "input" and aps.PYTHON_QUESTION in (e.get("label") or ""))
            await session.call("fill", key=field["key"], text="11")  # the person types it in the window
            with sessions() as db:
                db.info["user_id"] = user
                application_execution.continue_after_takeover(db, task.id)
            await aps.drive_async(sessions, passes=6)
            await manager.close(user, task.id)
            return loads, [p for m, p in ats.requests if m == "GET" and "dynamic_form" in p]
        before, after = asyncio.run(go())
        posts = list(ats.posts)
    bus.set_sidecars(None)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT"), \
        (task.result_code, task.last_error)
    assert after == before  # continued in the same page: never reloaded, the typed answer kept
    field_id = next(f["id"] for f in task.form["fields"] if f["label"] == aps.PYTHON_QUESTION)
    assert task.plan["fields"][field_id]["status"] == "PERSON"
    assert posts == []


def _auto_submit(monkeypatch, tmp_path, mode: str = "guarded_auto") -> None:
    aps.configure(monkeypatch, mode)
    for name, value in (("browser_use_webui_enabled", True), ("application_allow_private_hosts", True),
                        ("mcp_packet_dir", str(tmp_path / "packets"))):
        monkeypatch.setattr(aps.get_settings(), name, value)


def test_auto_submit_on_a_board_no_adapter_knows_submits_once_after_approval(runner, pg_sessions, pg_engine,
                                                                             monkeypatch, tmp_path):
    """Auto submit is on: approving the job queues the run by itself; the browser-use backend reveals and fills the
    form, attaches the approved resume, and clicks the form's own Submit once (signed, after the checks and the
    claim). Applied is recorded from the site's confirmation, as a verified submission."""
    from app.models import Job, SubmissionIntent

    _auto_submit(monkeypatch, tmp_path)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    sessions = qs.app_sessions(pg_engine)
    with gs.FakeATS("confirm") as ats:
        user = aps.owner(sessions)
        aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
        gs.consent(sessions, user)  # Settings → Auto submit
        job_id = aps.approved_job(sessions, user, ats.url("dynamic_form"))  # nothing else: approval queues it
        assert aps.task_of(sessions, job_id).mode == "GUARDED_AUTO"
        asyncio.run(aps.drive_async(sessions, passes=6))
        submits = list(ats.submits)
    task = aps.task_of(sessions, job_id)
    assert (task.backend, task.outcome) == ("browser_use_webui", "CONFIRMED"), (task.result_code, task.last_error)
    assert submits == ["/apply/submit"]
    with sessions() as db:
        job = db.get(Job, job_id)
        intent = db.scalar(select(SubmissionIntent).where(SubmissionIntent.execution_task_id == task.id))
        assert job.status == "applied" and intent.status == "CONFIRMED" and intent.backend == "browser_use_webui"
        assert intent.evidence["form_gone"] and intent.evidence["confirmation_wording"]
    assert bus.get_sidecars().get(user, task.id) is None  # the window was closed after the confirmation


class _VisibleWindow(bus.SidecarManager):
    """The real runner, headless in tests, treated as a visible window so the run may hand it to a person."""

    async def start(self, **kwargs):
        session = await super().start(**{**kwargs, "headless": True})
        session.headless = False
        return session


SELF_SUBMIT_PAGE = b"""<!doctype html><html><head><title>Apply</title></head><body>
<form id="apply" action="/apply/submit" method="post">
  <div class="field"><label for="name">Full name *</label><input type="text" id="name" name="name" required></div>
  <div class="field"><label for="mail">Email address *</label><input type="email" id="mail" name="mail" required></div>
  <button type="submit">Submit application</button>
</form>
<a href="#" id="send" onclick="fetch('/apply/submit', {method: 'POST'}).then(function () {
  location.href = '/confirmation.html'; }); return false">Send it myself</a>
</body></html>"""


def test_a_submission_the_person_makes_in_the_window_is_recorded_from_the_sites_confirmation(
        runner, pg_sessions, pg_engine, monkeypatch, tmp_path):
    """A filled form handed to the person works like a normal browser: when they submit there, the next pass sees the
    site's confirmation in that window and records Applied, without Start submission or "I applied"."""
    from app.models import Job
    from app.services import application_execution

    _auto_submit(monkeypatch, tmp_path, "assist")
    monkeypatch.setattr(aps.get_settings(), "browser_use_headless", False)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    sessions = qs.app_sessions(pg_engine)
    manager = _VisibleWindow()
    bus.set_sidecars(manager)
    with gs.FakeATS("confirm", pages={"self_submit.html": SELF_SUBMIT_PAGE}) as ats:
        user = aps.owner(sessions)
        job_id = aps.approved_job(sessions, user, ats.url("self_submit"))
        aps.enqueue(sessions, user, job_id)

        async def go():
            await aps.drive_async(sessions, passes=6)
            task = aps.task_of(sessions, job_id)
            assert (task.stage, task.outcome) == ("READY_TO_SUBMIT", "READY_TO_SUBMIT"), task.result_code
            session = manager.get(user, task.id)
            assert session is not None and session.parked_until is not None  # handed over, open
            page = await session.call("observe")
            send = next(e for e in page["elements"] if e["label"] == "Send it myself")
            await session.call("click", key=send["key"])  # the person submits, in the window
            await asyncio.sleep(1.0)

            def watch():
                with sessions() as db:
                    return application_execution.watch_windows(db)
            seen = await asyncio.to_thread(watch)
            await manager.close(user, task.id)
            return seen
        recorded = asyncio.run(go())
        submits = list(ats.submits)
    assert recorded == 1 and submits == ["/apply/submit"]
    task = aps.task_of(sessions, job_id)
    assert task.result_code == application_execution.SUBMITTED_IN_WINDOW
    with sessions() as db:
        assert db.get(Job, job_id).status == "applied"


# A challenge widget shaped like reCAPTCHA v2 (its iframe path and response field), served locally. The "solve" button
# stands in for the person ticking the box; the runner reports booleans only and never the response token.
CAPTCHA_PAGE = b"""<!doctype html><html><head><title>Apply</title></head><body>
<form id="apply"><label for="n">Full name *</label><input id="n" name="n" required>
<iframe src="/recaptcha/api2/anchor?k=test&size=normal" style="width:304px;height:78px"></iframe>
<textarea id="g-recaptcha-response" name="g-recaptcha-response" style="display:none"></textarea>
<button type="submit">Submit application</button></form>
<button type="button" id="solve" onclick="document.getElementById('g-recaptcha-response').value='SYNTHETIC-TOKEN-1'">
Solve</button></body></html>"""
INVISIBLE_PAGE = b"""<!doctype html><html><body><form><input name="n">
<iframe src="/recaptcha/api2/anchor?k=test&size=invisible" style="width:256px;height:60px"></iframe></form></body></html>"""
EMBEDDED_PAGE = b"""<!doctype html><html><body><h1>Careers</h1>
<iframe src="/captcha.html" style="width:800px;height:600px"></iframe></body></html>"""


def test_the_runner_reads_a_captcha_in_any_frame_and_only_whether_it_is_answered(runner, tmp_path):
    from app.services import application_execution, ats_forms

    manager = bus.get_sidecars()
    pages = {"captcha.html": CAPTCHA_PAGE, "invisible.html": INVISIBLE_PAGE, "embedded.html": EMBEDDED_PAGE}
    with gs.FakeATS("confirm", pages=pages) as ats, application_execution.background_loop() as loop:
        run = application_execution.loop_runner(loop, timeout=90)
        session = run(manager.start(user_id=1, task_id=20, job_id=5, allowed_hosts=["127.0.0.1"],
                                    upload_dir=str(tmp_path), allow_private=True))
        try:
            run(session.call("goto", url=ats.url("captcha")))
            before = run(session.call("observe", html=True))
            solve = next(e for e in before["elements"] if e.get("label", "").strip() == "Solve")
            run(session.call("click", key=solve["key"]))
            after = run(session.call("challenge"))
            evidence = run(session.call("evidence"))
            run(session.call("goto", url=ats.url("invisible")))
            invisible = run(session.call("challenge"))
            run(session.call("goto", url=ats.url("embedded")))
            embedded = run(session.call("challenge"))
        finally:
            run(manager.close(1, 20))
    assert before["captcha"] == {"present": True, "solved": False, "kind": "recaptcha"}
    assert ats_forms.page_problem(before["html"], title=before["title"], captcha=before["captcha"]) == "CAPTCHA_OR_ANTI_BOT"
    assert after == {"present": True, "solved": True, "kind": "recaptcha"}
    assert ats_forms.page_problem(before["html"], title=before["title"], captcha=after) is None
    assert evidence["captcha"]["solved"] and "SYNTHETIC-TOKEN-1" not in str([after, evidence])
    assert invisible["present"] is False  # reCAPTCHA v3: the site runs it itself; nobody is asked to solve it
    assert embedded == {"present": True, "solved": False, "kind": "recaptcha"}  # inside an embedded board's frame
