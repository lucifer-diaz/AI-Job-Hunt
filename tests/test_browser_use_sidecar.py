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
