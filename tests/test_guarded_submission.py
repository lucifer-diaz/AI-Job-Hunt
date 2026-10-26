"""Guarded submission (Phase 6), end to end: the real scheduler pass, the real graph, real Chromium and a local fake ATS
(tests/guarded_support.py). PostgreSQL (TEST_DATABASE_URL) and Chromium are required; otherwise skipped.

What each test proves, in the words of the safety contract:
  * a guarded run clicks once, holds the shared claim and records Applied only on the site's confirmation, through the
    one recorder, with READY_TO_APPLY → APPLYING → APPLIED and verified provenance;
  * an ambiguous or failed confirmation, a crash after the click, a kill or a lease running out leaves the outcome
    UNCERTAIN: never retried, never "not submitted", until a person says what happened;
  * a crash before the click releases the claim and the next attempt submits exactly once;
  * revoked consent, a changed packet, an unapproved legal attestation or a route off the allowlist means no click;
  * while a guarded run owns the posting, Claude Desktop and the Apply kit cannot start a submission beside it.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents import checkpoint
from app.infrastructure.application_browser import set_application_sessions
from app.infrastructure.application_browser.ephemeral import EphemeralApplicationSessions
from app.infrastructure.application_browser.guarded_submit import (
    GuardedSessions,
    set_guarded_sessions,
)
from app.infrastructure.browser import playwright_service as pw
from app.models import (
    ApplicationClaim,
    ApplicationEvent,
    ApplicationExecutionTask,
    Job,
    ResumeVariant,
    SubmissionIntent,
    WorkflowTransition,
)
from app.services import application_execution, apply_queue, submission
from app.services.lifecycle import InvalidTransition
from tests import application_support as aps
from tests import guarded_support as gs
from tests import qualification_support as qs

needs_chromium = pytest.mark.skipif(not (pw.playwright_installed() and pw.chromium_installed()),
                                    reason="Chromium is not installed (python -m playwright install chromium)")
pytestmark = needs_chromium


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch, tmp_path):
    gs.configure(monkeypatch)
    monkeypatch.setattr(aps.get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))
    monkeypatch.setattr(aps.get_settings(), "application_execution_retry_seconds", 0)
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    return qs.app_sessions(pg_engine)


def run(sessions, *steps):
    """Run the scheduler pass (and anything else) in one event loop with one real Chromium."""
    browser = pw.PlaywrightBrowserService(allow_private_hosts=True, record_telemetry=False,
                                          navigation_timeout_seconds=20)
    set_application_sessions(EphemeralApplicationSessions(browser, record_telemetry=False))
    set_guarded_sessions(GuardedSessions(browser, record_telemetry=False))

    async def go():
        try:
            for step in steps or (None,):
                if step is None:
                    await aps.drive_async(sessions, passes=8)
                else:
                    step()
                    await aps.drive_async(sessions, passes=8)
        finally:
            await browser.stop()
    try:
        asyncio.run(go())
    finally:
        set_application_sessions(None)
        set_guarded_sessions(None)


def crash_at(monkeypatch, point: str, then=None):
    """The worker dies at `point` of the SUBMIT node (once): the attempt fails and the task is retried."""
    from app.agents import application as graph

    original = graph._hold
    fired = {"done": False}

    def hold(ctx, at):
        if at == point and not fired["done"]:
            fired["done"] = True
            if then is not None:
                then()
            raise RuntimeError(f"worker died at {at}")
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)


def guarded_job(sessions, ats, page: str = "greenhouse_form") -> tuple[int, int]:
    user = gs.guarded_account(sessions)
    job_id = aps.approved_job(sessions, user, ats.url(page))
    aps.enqueue(sessions, user, job_id, mode="GUARDED_AUTO")
    return user, job_id


def facts(sessions, job_id):
    with sessions() as db:
        job = db.get(Job, job_id)
        task = db.scalar(select(ApplicationExecutionTask).where(ApplicationExecutionTask.job_id == job_id)
                         .order_by(ApplicationExecutionTask.id.desc()).limit(1))
        intents = db.scalars(select(SubmissionIntent).where(SubmissionIntent.job_id == job_id)
                             .order_by(SubmissionIntent.id)).all()
        claims = db.scalars(select(ApplicationClaim).where(ApplicationClaim.job_id == job_id)
                            .order_by(ApplicationClaim.id)).all()
        moves = [(t.from_state, t.to_state, t.actor) for t in db.scalars(
            select(WorkflowTransition).where(WorkflowTransition.job_id == job_id)
            .order_by(WorkflowTransition.id)).all()]
        out = {"job_status": job.status, "state": job.state,
               "task": (task.status, task.stage, task.outcome) if task else None,
               "intents": [i.status for i in intents], "claims": [(c.status, c.client, c.execution_id) for c in claims],
               "moves": moves, "task_id": task.id if task else None, "application": job.application.status,
               "confirmation": job.application.confirmation_id,
               "evidence": intents[-1].evidence if intents else None}
        out["applied_meta"] = next((e.meta for e in db.scalars(select(ApplicationEvent).where(
            ApplicationEvent.application_id == job.application.id, ApplicationEvent.kind == "applied")).all()), None)
        return out


# ───────────── the guarded path ─────────────

def test_a_guarded_run_submits_once_and_records_only_the_confirmed_submission(sessions):
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        posts = list(ats.posts)
    f = facts(sessions, job_id)
    assert f["task"] == ("COMPLETED", "CONFIRMED", "CONFIRMED")
    assert posts == ["/apply/submit"]  # one submission; the page's own load-time beacon was aborted
    assert f["job_status"] == "applied" and f["application"] == "applied" and f["state"] == "APPLIED"
    assert f["moves"][-2:] == [("READY_TO_APPLY", "APPLYING", "application_worker"),
                               ("APPLYING", "APPLIED", "application_worker")]
    assert f["intents"] == ["CONFIRMED"]
    assert f["claims"] == [("RECORDED", "worker", f["task_id"])]  # the shared claim, bound to the execution
    assert f["confirmation"] == "GH-48213"
    assert f["applied_meta"]["evidence"] == "verified_browser" and f["applied_meta"]["claim"] == "claimed"
    assert f["evidence"]["form_gone"] and f["evidence"]["confirmation_wording"]
    assert "asha" not in str(f["evidence"]).lower()  # metadata only


@pytest.mark.parametrize("answer", ["ambiguous", "error"])
def test_an_unconfirmed_click_is_uncertain_and_is_never_retried(sessions, answer):
    with gs.FakeATS(answer) as ats:
        user, job_id = guarded_job(sessions, ats)
        run(sessions)
        f = facts(sessions, job_id)
        assert f["task"] == ("COMPLETED", "SUBMISSION_UNCERTAIN", "SUBMISSION_UNCERTAIN")
        assert f["intents"] == ["UNCERTAIN"] and f["claims"][0][0] == "UNCERTAIN"
        assert f["state"] == "APPLYING" and f["job_status"] == "approved"  # never recorded, never "not submitted"
        with sessions() as db:
            db.info["user_id"] = user
            job = db.get(Job, job_id)
            with pytest.raises(application_execution.ExecutionRefused) as again:
                application_execution.enqueue(db, job, execution_mode="GUARDED_AUTO")
            assert again.value.code == "CLAIMED"
            assert apply_queue.begin_submission(db, job, client="mcp").status == "UNCERTAIN"
            from app.services.work_queue import transition
            with pytest.raises(InvalidTransition):  # only a person moves it back
                transition(db, job, "READY_TO_APPLY", actor="application_worker")
            db.rollback()
        run(sessions)  # more scheduler passes change nothing
        submits = list(ats.submits)
    assert submits == ["/apply/submit"]
    with sessions() as db:
        db.info["user_id"] = user
        claim = db.scalar(select(ApplicationClaim).where(ApplicationClaim.job_id == job_id))
        apply_queue.resolve_uncertain(db, db.get(Job, job_id), claim.id, submitted=False)
    f = facts(sessions, job_id)
    assert f["state"] == "READY_TO_APPLY" and f["moves"][-1] == ("APPLYING", "READY_TO_APPLY", "person")
    assert f["intents"] == ["RESOLVED_NOT_SUBMITTED"]


def test_a_crash_after_the_click_is_uncertain_and_clicks_nothing_again(sessions, monkeypatch):
    crash_at(monkeypatch, "after_click")
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == ["/apply/submit"]
    assert f["task"][1:] == ("SUBMISSION_UNCERTAIN", "SUBMISSION_UNCERTAIN")
    assert f["intents"] == ["UNCERTAIN"] and f["state"] == "APPLYING" and f["job_status"] == "approved"


def test_a_crash_before_the_click_gives_the_claim_back_and_submits_exactly_once(sessions, monkeypatch):
    crash_at(monkeypatch, "armed")
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == ["/apply/submit"]
    assert f["intents"] == ["ABORTED", "CONFIRMED"]
    assert [c[0] for c in f["claims"]] == ["RELEASED", "RECORDED"]
    assert f["task"][2] == "CONFIRMED" and f["state"] == "APPLIED"


def test_the_lease_running_out_after_the_click_is_uncertain(sessions, monkeypatch):
    from datetime import timedelta

    def expire():
        with sessions() as db:
            task = db.scalar(select(ApplicationExecutionTask))
            task.lock_expires_at = aps.utcnow() - timedelta(seconds=1)
            db.commit()
            application_execution.recover_expired(db)

    crash_at(monkeypatch, "after_click", then=expire)
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == ["/apply/submit"]
    assert f["task"] == ("COMPLETED", "SUBMISSION_UNCERTAIN", "SUBMISSION_UNCERTAIN")
    assert f["intents"] == ["UNCERTAIN"] and f["claims"][0][0] == "UNCERTAIN"


# ───────────── what stops the click ─────────────

def test_consent_revoked_after_the_claim_means_no_click(sessions, monkeypatch):
    holder = {}

    def revoke():
        gs.consent(sessions, holder["user"], enabled=False)

    from app.agents import application as graph

    original = graph._hold

    def hold(ctx, at):
        if at == "armed":
            revoke()
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        holder["user"], job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == []
    assert f["task"] == ("COMPLETED", "NEEDS_HUMAN", "NEEDS_HUMAN")
    assert f["intents"] == ["ABORTED"] and [c[0] for c in f["claims"]] == ["RELEASED"]
    assert f["state"] == "READY_TO_APPLY" and f["job_status"] == "approved"



def test_consent_revoked_after_clicking_was_authorized_still_stops_the_click(sessions, monkeypatch):
    """The ticket's last check, immediately before the click, reads the account's consent again."""
    holder = {}
    from app.agents import application as graph

    original = graph._hold

    def hold(ctx, at):
        if at == "before_click":
            gs.consent(sessions, holder["user"], enabled=False)
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        holder["user"], job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == []
    assert f["intents"] == ["ABORTED"] and [c[0] for c in f["claims"]] == ["RELEASED"]
    assert f["state"] == "READY_TO_APPLY" and f["job_status"] == "approved"


def test_a_packet_changed_after_the_claim_means_no_click(sessions, monkeypatch):
    from app.agents import application as graph
    from app.services import approval

    original, seen = graph._hold, {}

    def hold(ctx, at):
        if at == "armed":
            with sessions() as db:
                variant = db.scalar(select(ResumeVariant))
                with pytest.raises(ValueError):  # the shared guard refuses an edit while the submission is open
                    approval.guard_edit(db, variant, "the resume")
                variant.markdown += "\n- A claim added behind the approval's back"
                db.commit()
            seen["edited"] = True
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert seen and submits == []
    assert f["intents"] == ["ABORTED"] and f["task"][1] == "BLOCKED"



def test_an_intent_aborted_by_someone_else_is_never_turned_into_a_click(sessions, monkeypatch):
    """A Kill or the lease sweep aborts the ARMED intent while the worker is between its checks and the CLICKING
    commit: the commit is a compare-and-set from ARMED, so the worker refuses instead of overwriting ABORTED."""
    from app.agents import application as graph

    original = graph._hold

    def hold(ctx, at):
        if at == "armed":
            with sessions() as db:  # the claim is not released yet: only the intent's own status stops the click
                intent = db.scalar(select(SubmissionIntent))
                assert submission.move(db, intent.id, submission.ABORTED, only_from=(submission.ARMED,))
                db.commit()
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == []
    assert f["intents"] == ["ABORTED"] and f["state"] == "READY_TO_APPLY" and f["job_status"] == "approved"


def test_an_abort_or_release_arriving_after_clicking_changes_nothing(sessions, monkeypatch):
    """The reverse race: CLICKING is committed, then a late abort (a Kill or sweep that read ARMED earlier) or a
    release arrives. Neither may free the claim of a click that may happen: the submission is recorded once."""
    from app.agents import application as graph

    original, seen = graph._hold, {}

    def hold(ctx, at):
        if at == "before_click":
            with sessions() as db:
                intent = db.scalar(select(SubmissionIntent))
                job = db.get(Job, intent.job_id)
                claim = db.get(ApplicationClaim, intent.claim_id)
                seen["aborted"] = submission.abort(db, intent, job, "late abort")
                with pytest.raises(ValueError):
                    apply_queue.release_claim(db, job, claim.token, "late release")
            with sessions() as db:
                seen["after"] = (db.scalar(select(SubmissionIntent)).status, db.scalar(select(ApplicationClaim)).status)
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        _user, job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert seen["aborted"] is False and seen["after"] == ("CLICKING", "CLAIMED")
    assert submits == ["/apply/submit"]
    assert f["intents"] == ["CONFIRMED"] and f["state"] == "APPLIED"


LEGAL_FORM = (aps.FORMS / "greenhouse_form.html").read_text().replace(
    '<input type="button" id="submit_app"',
    '<div class="field"><label for="certify">I certify that the information I have provided is true and complete *'
    '</label><input type="checkbox" id="certify" name="job_application[certify]" required></div>\n'
    '    <input type="button" id="submit_app"').encode()
CERTIFY = "I certify that the information I have provided is true and complete"


def test_an_unapproved_legal_attestation_waits_for_the_person(sessions):
    with gs.FakeATS("confirm", pages={"legal_form.html": LEGAL_FORM}) as ats:
        user = gs.guarded_account(sessions)
        aps.answer(sessions, user, CERTIFY, "Yes")
        job_id = aps.approved_job(sessions, user, ats.url("legal_form"))
        aps.enqueue(sessions, user, job_id, mode="GUARDED_AUTO")
        run(sessions)
        f = facts(sessions, job_id)
        assert ats.submits == [] and f["task"] == ("COMPLETED", "NEEDS_HUMAN", "NEEDS_HUMAN")
        with sessions() as db:
            assert db.scalar(select(ApplicationExecutionTask)).result_code == "LEGAL_ATTESTATION"
        gs.consent(sessions, user, attestations=[CERTIFY])  # the person approves exactly this wording
        aps.enqueue(sessions, user, job_id, mode="GUARDED_AUTO")
        run(sessions)
        submits = list(ats.submits)
    assert submits == ["/apply/submit"] and facts(sessions, job_id)["state"] == "APPLIED"


def test_guarded_mode_is_refused_without_consent_an_allowlisted_route_or_the_server_mode(sessions, monkeypatch):
    settings = aps.get_settings()
    with gs.FakeATS("confirm") as ats:
        user = aps.owner(sessions)
        aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
        job_id = aps.approved_job(sessions, user, ats.url())
        steps = [("NO_CONSENT", lambda: None),
                 ("ROUTE_NOT_ALLOWED", lambda: (gs.consent(sessions, user),
                                                monkeypatch.setattr(settings, "application_guarded_routes", "lever"))),
                 ("MODE", lambda: (monkeypatch.setattr(settings, "application_guarded_routes", "greenhouse"),
                                   monkeypatch.setattr(settings, "application_execution_mode", "assist")))]
        for code, setup in steps:
            setup()
            with pytest.raises(application_execution.ExecutionRefused) as refused:
                aps.enqueue(sessions, user, job_id, mode="GUARDED_AUTO")
            assert refused.value.code == code
        assert ats.requests == []


# ───────────── one submitter per posting ─────────────

def test_while_a_guarded_run_owns_the_posting_no_one_else_can_start_a_submission(sessions, monkeypatch):
    from app.agents import application as graph
    from app.mcp import tools
    from app.mcp.tools import ToolError

    original, seen = graph._hold, {}

    def hold(ctx, at):
        if at == "armed":
            with sessions() as db:
                db.info["user_id"] = seen["user"]
                job = db.get(Job, seen["job"])
                seen["mcp_begin"] = tools.begin_application_submission(db, job.id)["status"]
                with pytest.raises(ToolError) as packet:
                    tools.get_job_packet(db, job.id)
                seen["mcp_packet"] = "ROUTE_HELD" in str(packet.value)
                seen["kit"] = apply_queue.begin_submission(db, db.get(Job, seen["job"]), client="apply_kit").status
                seen["report"] = apply_queue.record_submission(db, db.get(Job, seen["job"]), client="apply_kit").status
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        seen["user"], seen["job"] = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    assert seen["mcp_begin"] == "ROUTE_HELD" and seen["mcp_packet"] and seen["kit"] == "ROUTE_HELD"
    assert seen["report"] == "CLAIM_CONFLICT"  # a tokenless "I applied" cannot slip in beside the worker
    assert submits == ["/apply/submit"] and facts(sessions, seen["job"])["state"] == "APPLIED"


def test_a_confirmed_posting_is_never_submitted_again_by_any_path(sessions):
    with gs.FakeATS("confirm") as ats:
        user, job_id = guarded_job(sessions, ats)
        run(sessions)
        alias = aps.approved_job(sessions, user, ats.url() + "?utm_source=board")  # the same posting, saved twice
        with sessions() as db:
            db.info["user_id"] = user
            for jid in (job_id, alias):
                with pytest.raises(application_execution.ExecutionRefused):
                    application_execution.enqueue(db, db.get(Job, jid), execution_mode="GUARDED_AUTO")
            assert apply_queue.begin_submission(db, db.get(Job, alias), client="mcp").status == "DUPLICATE"
        run(sessions)
        submits = list(ats.submits)
    assert submits == ["/apply/submit"]


def test_a_manual_report_takes_the_same_canonical_route_as_a_verified_one(sessions):
    """Parity: Claude Desktop's or a person's report also moves READY_TO_APPLY → APPLYING → APPLIED, labelled
    reported (never verified)."""
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, "https://boards.greenhouse.io/examplecorp/jobs/1")
    with sessions() as db:
        db.info["user_id"] = user
        assert apply_queue.record_submission(db, db.get(Job, job_id), client="mcp").status == "SUBMITTED"
        with pytest.raises(ValueError):  # nobody but the worker, with its claim, records a verified submission
            apply_queue.record_submission(db, db.get(Job, job_id), client="mcp",
                                          evidence={"kind": "verified_browser"})
    f = facts(sessions, job_id)
    assert f["state"] == "APPLIED" and f["moves"][-2:] == [("READY_TO_APPLY", "APPLYING", "claude_desktop"),
                                                           ("APPLYING", "APPLIED", "claude_desktop")]
    assert f["applied_meta"]["evidence"] == "reported"


def test_the_confirmation_verifier_needs_all_three_kinds_of_evidence():
    ok = {"url": "https://boards.greenhouse.io/acme/jobs/1/confirmation", "html": "<h1>Thank you for applying</h1>",
          "responses": [{"method": "POST", "status": 302, "host": "boards.greenhouse.io"}]}
    verify = submission.ConfirmationVerifier.verify
    assert verify("greenhouse", "boards.greenhouse.io", ok).confirmed
    still_form = {**ok, "html": ok["html"] + '<form id="application_form"></form>'}
    other_host = {**ok, "responses": [{"method": "POST", "status": 200, "host": "evil.example"}]}
    no_post = {**ok, "responses": []}
    failed = {**ok, "responses": [{"method": "POST", "status": 422, "host": "boards.greenhouse.io"}]}
    plain = {**ok, "url": "https://boards.greenhouse.io/acme/jobs/1", "html": "<p>Looks done!</p>"}
    for case in (still_form, other_host, no_post, failed, plain):
        assert not verify("greenhouse", "boards.greenhouse.io", case).confirmed


@pytest.mark.parametrize("point,intent,claim", [("armed", "ABORTED", "RELEASED"),
                                                ("before_click", "UNCERTAIN", "UNCERTAIN")])
def test_kill_stops_the_run_and_never_clicks(sessions, monkeypatch, point, intent, claim):
    """Kill before the click gives the claim back; Kill once the click was authorized (CLICKING committed) leaves the
    outcome to a person — a kill is never taken as "not submitted" — and the click itself is refused."""
    from app.agents import application as graph
    from app.services import workers

    original, holder = graph._hold, {}

    def hold(ctx, at):
        if at == point:
            with sessions() as db:
                db.info["user_id"] = holder["user"]
                workers.kill(db, holder["user"], "application")
        return original(ctx, at)
    monkeypatch.setattr(graph, "_hold", hold)
    with gs.FakeATS("confirm") as ats:
        holder["user"], job_id = guarded_job(sessions, ats)
        run(sessions)
        submits = list(ats.submits)
    f = facts(sessions, job_id)
    assert submits == []
    assert f["task"][0] == "SKIPPED" and f["intents"] == [intent] and f["claims"][0][0] == claim
    assert f["job_status"] == "approved"
