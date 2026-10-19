"""The application browser (Phase 5) with real Chromium against a local form server. Skipped when Chromium is not
installed (`python -m playwright install chromium`); the end-to-end run also needs PostgreSQL (TEST_DATABASE_URL).

  * an application session shares nothing with JD recovery's pages (cookies set in one never reach the other);
  * every non-GET request is aborted at the page: a beacon, and even a script that forces the form to submit;
  * a full ASSIST run through the real scheduler pass fills the form and ends READY_TO_SUBMIT with zero POSTs.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents import checkpoint
from app.infrastructure.application_browser import set_application_sessions
from app.infrastructure.application_browser.ephemeral import EphemeralApplicationSessions
from app.infrastructure.browser import playwright_service as pw
from app.models import AgentThread, Job
from tests import application_support as aps
from tests import qualification_support as qs

needs_chromium = pytest.mark.skipif(not (pw.playwright_installed() and pw.chromium_installed()),
                                    reason="Chromium is not installed (python -m playwright install chromium)")


def _browser() -> pw.PlaywrightBrowserService:
    return pw.PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False, navigation_timeout_seconds=20)


@needs_chromium
def test_application_sessions_share_nothing_with_jd_recovery_and_post_nothing():
    with aps.FormServer() as server:
        browser = _browser()
        sessions = EphemeralApplicationSessions(browser, record_telemetry=False)

        async def go():
            try:
                async with sessions.session(user_id=1, ats="greenhouse") as page:
                    await page.goto(server.url())
                    await asyncio.sleep(0.5)  # the page's beacon fires on load
                    own = await page._page.context.cookies()
                    # A page script forcing the form to submit (what a click on Submit would do): aborted too.
                    await page._page.evaluate("document.getElementById('application_form').submit()")
                    await asyncio.sleep(0.5)
                    blocked = page.blocked_requests
                async with browser.page_session() as jd_page:  # what JD recovery gets
                    jd_cookies = await jd_page.context.cookies()
                async with sessions.session(user_id=2, ats="greenhouse") as other:
                    other_cookies = await other._page.context.cookies()
                posted_by_application = list(server.posts)
                # Contrast: JD recovery's anonymous render runs the same page and its beacon does reach the server
                # (reading a posting is not applying). The application browser is the one that never posts.
                rendered = await browser.fetch_rendered_page(server.url())
                await asyncio.sleep(0.5)
                return own, blocked, jd_cookies, other_cookies, rendered, posted_by_application
            finally:
                await browser.stop()

        own, blocked, jd_cookies, other_cookies, rendered, posted = asyncio.run(go())
        all_posts = list(server.posts)
    assert [c["name"] for c in own] == ["application_session"]
    assert jd_cookies == [] and other_cookies == []  # nothing survives a session, or crosses into JD recovery
    assert blocked >= 2  # the beacon and the forced submit
    assert posted == []  # the beacon and the forced form submission never reached the server
    assert "/apply/submit" not in all_posts and "Senior Backend Engineer" in rendered.html


@needs_chromium
def test_a_full_assist_run_in_chromium_ends_ready_to_submit_with_zero_posts(pg_sessions, pg_engine, monkeypatch,
                                                                           tmp_path):
    aps.configure(monkeypatch, "assist")
    monkeypatch.setattr(aps.get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    sessions = qs.app_sessions(pg_engine)
    with aps.FormServer() as server:
        user = aps.owner(sessions)
        ids = aps.standard_answers(sessions, user)
        job_id = aps.approved_job(sessions, user, server.url())
        aps.enqueue(sessions, user, job_id)
        browser = _browser()
        set_application_sessions(EphemeralApplicationSessions(browser, record_telemetry=False))
        try:
            async def run():
                try:
                    await aps.drive_async(sessions)
                    task = aps.task_of(sessions, job_id)
                    assert task.stage == "NEEDS_INPUT" and task.interrupt["confirm_question_ids"] == [ids["auth"]]
                    with sessions() as db:
                        db.info["user_id"] = user
                        from app.services import application_execution

                        application_execution.confirm_answers(db, task.id, [ids["auth"]])
                    await aps.drive_async(sessions)
                finally:
                    await browser.stop()

            asyncio.run(run())
        finally:
            set_application_sessions(None)
        posts = server.posts
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT")
    assert posts == []  # nothing — not the form, not the page's beacon — was ever posted
    with sessions() as db:
        thread = db.get(AgentThread, task.agent_thread_id)
        assert thread.result["missing_required_count"] == 0 and thread.result["filled_count"] >= 7
        assert thread.result["blocked_requests"] >= 1  # the page tried to post; the browser refused
        job = db.scalar(select(Job).where(Job.id == job_id))
        assert job.status == "approved"  # a person submits; nothing was recorded as applied
