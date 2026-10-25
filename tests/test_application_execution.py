"""The application execution worker (Phase 5), on PostgreSQL (TEST_DATABASE_URL; skipped otherwise).

Each test drives the real `application_execution` scheduler pass and graph over a fixture Greenhouse-shaped form served
by a fake application browser (tests/application_support.py), and checks the task, its attempts, the job and what was
filled. Nothing is ever submitted: the last stage a run can reach is READY_TO_SUBMIT.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.agents import checkpoint
from app.infrastructure.application_browser import set_application_sessions
from app.models import (
    AgentThread,
    Application,
    ApplicationClaim,
    ApplicationExecutionAttempt,
    ApplicationExecutionTask,
    Job,
    KnowledgeAnswer,
    ResumeVariant,
)
from app.services import application_execution, apply_queue, workers
from app.services.lifecycle import STOPPED_BY_USER
from tests import application_support as aps
from tests import qualification_support as qs

FORM = "https://boards.greenhouse.io/examplecorp/jobs/4012345"


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch, tmp_path):
    aps.configure(monkeypatch, "assist")
    monkeypatch.setattr(aps.get_settings(), "mcp_packet_dir", str(tmp_path / "packets"))
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    return qs.app_sessions(pg_engine)


@pytest.fixture
def browser():
    fake = aps.FakeSessions()
    fake.pages[FORM] = "greenhouse_form"
    set_application_sessions(fake)
    yield fake
    set_application_sessions(None)


def _waiting_then_confirm(sessions, user, job_id, ids):
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_INPUT")
    assert task.interrupt["confirm_question_ids"] == [ids["auth"]] and task.interrupt["question_ids"] == []
    with sessions() as db:
        db.info["user_id"] = user
        application_execution.confirm_answers(db, task.id, [ids["auth"]])
    return task


# ───────────── the dry run ─────────────

def test_assist_fills_the_form_and_stops_at_ready_to_submit(sessions, browser):
    user = aps.owner(sessions)
    ids = aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)

    first = _waiting_then_confirm(sessions, user, job_id, ids)  # the sensitive answer waits for the person
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "READY_TO_SUBMIT", "READY_TO_SUBMIT")
    assert task.id == first.id and task.route == "greenhouse" and task.attempt_count == 1  # waiting used no attempt

    (filled,) = browser.filled
    assert filled["#first_name"] == "Asha" and filled["#last_name"] == "Rao"
    assert filled["#email"] == "asha@example.test" and filled["#phone"] == "+65 5555 0100"
    assert filled["#resume"].endswith(".pdf") and filled["#cover_letter"] == "cover_letter.txt"
    assert filled["#job_application_answers_attributes_0_text_value"].endswith("asha-rao-test")
    assert filled["#job_application_answers_attributes_1_text_value"] == "8"
    assert filled["#job_application_answers_attributes_2_boolean_value"] == "1"  # the confirmed answer, as an option
    assert "#job_application_gender" not in filled  # voluntary self-identification is never answered
    assert "#job_application_answers_attributes_3_text_value" not in filled  # optional and unknown: left empty
    assert browser.opened == browser.closed == 2  # read once, filled once; every session closed

    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "approved" and job.application.status == "approved"  # nothing was recorded as applied
        assert db.scalar(select(func.count()).select_from(ApplicationClaim)) == 0  # the worker takes no claim
        (attempt,) = db.scalars(select(ApplicationExecutionAttempt)).all()
        assert attempt.outcome == "READY_TO_SUBMIT" and attempt.stage_reached == "READY_TO_SUBMIT"
        assert len(attempt.file_digests["pdf"]) == 64
        # The plan names sources, never values.
        plan = db.get(ApplicationExecutionTask, task.id).plan
        assert plan["fields"]["email"]["source"] == "candidate_profile.email"
        assert "asha@example.test" not in str(plan) and "+65" not in str(plan)
        thread = db.get(AgentThread, task.agent_thread_id)
        assert thread.status == "COMPLETED" and thread.result["outcome"] == "READY_TO_SUBMIT"
        assert "asha" not in str(thread.result).lower()


def test_observe_resolves_every_answer_and_fills_nothing(sessions, browser, monkeypatch):
    aps.configure(monkeypatch, "observe")
    user = aps.owner(sessions)
    ids = aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    _waiting_then_confirm(sessions, user, job_id, ids)
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome) == ("COMPLETED", "PRE_SUBMIT_VALIDATION", "OBSERVED")
    assert browser.filled == [] and browser.opened == 1


def test_assist_cannot_be_asked_for_on_an_observe_server(sessions, browser, monkeypatch):
    aps.configure(monkeypatch, "observe")
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    with pytest.raises(application_execution.ExecutionRefused) as refused:
        aps.enqueue(sessions, user, job_id, mode="ASSIST")
    assert refused.value.code == "MODE"


# ───────────── answers a person must give ─────────────

def test_an_unknown_required_answer_waits_for_the_person_and_resumes(sessions, browser):
    user = aps.owner(sessions)
    auth = aps.answer(sessions, user, aps.AUTH_QUESTION, "Yes")  # not sensitive here: no confirmation needed
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)

    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_INPUT")
    (question_id,) = task.interrupt["question_ids"]
    with sessions() as db:
        saved = db.get(KnowledgeAnswer, question_id)
        assert saved.question == aps.PYTHON_QUESTION and not saved.verified and saved.answer == ""
        assert saved.request_context["requested_by"] == "application_worker"
    assert browser.filled == []
    aps.drive(sessions)
    assert aps.task_of(sessions, job_id).stage == "NEEDS_INPUT"  # still waiting: nothing is claimed meanwhile

    with sessions() as db:  # the person answers it in the Knowledge bank (the same hook PUT /api/knowledge runs)
        db.info["user_id"] = user
        row = db.get(KnowledgeAnswer, question_id)
        row.answer, row.verified = "8", True
        application_execution.wake_waiting(db, user)
        db.commit()
    assert aps.task_of(sessions, job_id).stage == "ANSWERING"  # the answer woke it
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert task.stage == "READY_TO_SUBMIT" and auth


# ───────────── the approved packet ─────────────

def test_a_packet_changed_before_the_fill_blocks_the_run(sessions, browser):
    user = aps.owner(sessions)
    ids = aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    task = _waiting_then_confirm(sessions, user, job_id, ids)
    with sessions() as db:  # changed behind the guard (a direct write): the digest no longer matches
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id))
        variant.markdown += "\n- An edit nobody approved"
        db.commit()
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.result_code) == ("COMPLETED", "BLOCKED", "PACKET_CHANGED")
    assert browser.filled == []  # the form was never touched


def test_a_reapproval_after_enqueue_needs_a_new_run(sessions, browser):
    user = aps.owner(sessions)
    aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    with sessions() as db:
        db.info["user_id"] = user
        variant = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id))
        variant.cover_letter = "A different letter"
        db.commit()
        from app.services import workflow

        job = db.get(Job, job_id)
        job.application.status, job.status = "pending_approval", "tailored"
        db.commit()
        workflow.approve_job(db, job)
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.stage, task.result_code) == ("BLOCKED", "PACKET_CHANGED")


# ───────────── pages a person must handle ─────────────

@pytest.mark.parametrize(("page", "code", "blocker"), [
    ("greenhouse_login", "LOGIN_REQUIRED", "login_required"),
    ("greenhouse_otp", "OTP_REQUIRED", "login_required"),
    ("greenhouse_captcha", "CAPTCHA_OR_ANTI_BOT", "captcha"),
])
def test_a_login_code_or_challenge_pauses_for_a_person(sessions, browser, page, code, blocker):
    browser.pages[FORM] = page
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.outcome, task.result_code) == ("COMPLETED", "NEEDS_HUMAN", "NEEDS_HUMAN",
                                                                         code)
    assert len(browser.loads) == 1  # never retried, never worked around
    with sessions() as db:
        assert db.scalar(select(Application).where(Application.job_id == job_id)).apply_blocker["reason"] == blocker


def test_a_closed_posting_and_an_unknown_site_end_the_run(sessions, browser):
    user = aps.owner(sessions)
    browser.pages[FORM] = "greenhouse_closed"
    closed = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, closed)
    aps.drive(sessions)
    task = aps.task_of(sessions, closed)
    assert (task.stage, task.result_code) == ("BLOCKED", "POSTING_CLOSED")
    with sessions() as db:
        assert db.scalar(select(Application).where(Application.job_id == closed)).apply_blocker["reason"] == \
            "posting_closed"


def test_a_site_without_a_known_form_is_unsupported(sessions, browser):
    user = aps.owner(sessions)
    url = "https://careers.example.test/jobs/7"
    browser.pages[url] = "careers_page"
    job_id = aps.approved_job(sessions, user, url)
    aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.stage, task.outcome, task.result_code) == ("BLOCKED", "UNSUPPORTED", "UNSUPPORTED_SITE")
    with sessions() as db:  # nothing a person must handle: the Apply kit and Claude Desktop can still apply
        assert db.scalar(select(Application).where(Application.job_id == job_id)).apply_blocker is None


def test_a_form_that_changed_between_reading_and_filling_is_not_filled(sessions, browser):
    user = aps.owner(sessions)
    ids = aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    _waiting_then_confirm(sessions, user, job_id, ids)
    browser.pages[FORM] = "greenhouse_changed"
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.stage, task.result_code) == ("BLOCKED", "FORM_CHANGED") and browser.filled == []


# ───────────── Running workers: kill, retry, leases ─────────────

def test_kill_stops_the_task_and_its_thread_and_retry_queues_a_new_one(sessions, browser):
    user = aps.owner(sessions)
    aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    first = aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)  # waiting for the confirmation, its thread interrupted
    with sessions() as db:
        db.info["user_id"] = user
        out = workers.kill(db, user, "application", [first])
    assert out["stopped"] == 1
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.last_error, task.stage, task.outcome) == ("SKIPPED", STOPPED_BY_USER, "CANCELLED",
                                                                        "STOPPED")
    with sessions() as db:
        assert db.get(AgentThread, task.agent_thread_id).status == "STOPPED"
    assert aps.drive(sessions)[0].get("claimed", 0) == 0  # nothing revives it

    with sessions() as db:
        db.info["user_id"] = user
        result = workers.retry(db, user, "application", [first])
    assert result["retried"] == [first] or result["retried"]
    fresh = aps.task_of(sessions, job_id)
    assert fresh.id != first and fresh.status == "PENDING" and fresh.stage == "QUEUED"


def test_an_expired_lease_retries_and_never_moves_towards_a_submit(sessions, browser):
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    task_id = aps.enqueue(sessions, user, job_id)
    with sessions() as db:
        (claim,) = application_execution.claim(db, worker_id="dead-worker", limit=1)
        task = db.get(ApplicationExecutionTask, task_id)
        task.lock_expires_at = aps.utcnow().replace(year=2020)
        db.commit()
        assert application_execution.recover_expired(db) == 1
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "RETRYING") and claim.task_id == task_id


def test_a_run_waiting_for_answers_says_so_and_shows_no_model_in_running_workers(sessions, browser):
    """A run paused for the person is not "waiting its turn": it says what it needs, cannot be Run now, and a
    Greenhouse (form adapter) run never shows a model it does not call."""
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)  # the Python question has no verified answer and the sensitive one needs confirming
    with sessions() as db:
        db.info["user_id"] = user
        (row,) = workers.rows(db, user, "application")["rows"]
    assert row["status"] == "QUEUED" and row["waiting_for"] == "YOU" and row["can_run_now"] is False
    assert row["waiting_detail"]["kind"] == "NEEDS_INPUT" and row["waiting_detail"]["questions"] >= 1
    assert row["backend"] == "playwright" and row["model"] == {"model": None, "source": "none"}
    assert row["model_change"] is None
    # The Apply kit asks exactly those questions in place, with the form's own options.
    with sessions() as db:
        db.info["user_id"] = user
        task = db.get(ApplicationExecutionTask, row["task_id"])
        asked = application_execution.questions(db, task)
    assert [q["id"] for q in asked] == task.interrupt["question_ids"]
    assert aps.PYTHON_QUESTION in [q["question"] for q in asked] and not any(q["answered"] for q in asked)
    with sessions() as db:  # answering every one of them, the way the Apply kit does, wakes the run
        db.info["user_id"] = user
        for q in asked:
            saved = db.get(KnowledgeAnswer, q["id"])
            saved.answer = "8" if q["question"] == aps.PYTHON_QUESTION else (q["answer_options"] or ["Yes"])[0]
            saved.verified = True
        db.flush()
        assert application_execution.wake_waiting(db, user) == 1
        db.commit()
    assert aps.task_of(sessions, job_id).stage == "ANSWERING"


def test_the_application_worker_is_visible_in_running_workers_for_its_owner_only(sessions, browser):
    user, other = aps.owner(sessions), aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    task_id = aps.enqueue(sessions, user, job_id)
    with sessions() as db:
        db.info["user_id"] = user
        mine = workers.rows(db, user, "application")
        assert [t["task_id"] for t in mine["rows"]] == [task_id] and mine["rows"][0]["status"] == "QUEUED"
        assert workers.counts(db, user)["application"]["queued"] == 1
    with sessions() as db:
        db.info["user_id"] = other
        assert workers.rows(db, other, "application")["rows"] == []
        with pytest.raises(workers.WorkerError) as refused:
            workers.run_now(db, other, "application", task_id)
        assert refused.value.status == 404
    with sessions() as db:
        db.info["user_id"] = user
        assert workers.run_now(db, user, "application", task_id)["task_id"] == task_id


def test_a_restart_resumes_a_waiting_run_from_its_checkpoint(sessions, browser):
    user = aps.owner(sessions)
    ids = aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    task = _waiting_then_confirm(sessions, user, job_id, ids)
    checkpoint.forget_setup()  # a new process: nothing in memory
    aps.drive(sessions)
    resumed = aps.task_of(sessions, job_id)
    assert resumed.agent_thread_id == task.agent_thread_id and resumed.stage == "READY_TO_SUBMIT"
    assert browser.opened == 2  # the form was read once, before the restart; filled once after it


def test_enqueue_follows_the_shared_readiness_rules(sessions, browser):
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    with sessions() as db:
        db.info["user_id"] = user
        claim = apply_queue.begin_submission(db, db.get(Job, job_id), client="apply_kit")
        assert claim.ok
    with pytest.raises(application_execution.ExecutionRefused) as refused:
        aps.enqueue(sessions, user, job_id)
    assert refused.value.code == "CLAIMED"
    with sessions() as db:
        db.info["user_id"] = user
        draft = Job(user_id=user, source="test", source_ref="draft", title="T", company="C", url=FORM + "?x=1",
                    status="tailored")
        db.add(draft)
        db.commit()
        draft_id = draft.id
    with pytest.raises(application_execution.ExecutionRefused) as refused:
        aps.enqueue(sessions, user, draft_id)
    assert refused.value.code == "NOT_READY"


def test_mode_off_ends_the_tasks_left_over(sessions, browser, monkeypatch):
    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    monkeypatch.setattr(aps.get_settings(), "application_execution_mode", "off")
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage, task.result_code) == ("SKIPPED", "CANCELLED", application_execution.MODE_OFF)



# ───────────── review fixes (Codex review of PR #88) ─────────────

def test_a_sensitive_answer_to_a_merely_similar_question_is_never_filled(sessions, browser):
    """"…authorized to work in Singapore without sponsorship?" is similar enough to the form's "…authorized to work in
    Singapore?" for knowledge.search to withhold it as NEEDS_CONFIRMATION, but its "No" means something else here.
    The worker asks the form's own question instead of offering the similar one for confirmation."""
    user = aps.owner(sessions)
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    similar = aps.answer(sessions, user, "Are you legally authorized to work in Singapore without sponsorship?", "No",
                         sensitive=True)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.stage) == ("PENDING", "NEEDS_INPUT")
    assert task.interrupt["confirm_question_ids"] == []  # nothing similar is offered for confirmation
    (asked,) = task.interrupt["question_ids"]
    assert asked != similar
    with sessions() as db:
        assert db.get(KnowledgeAnswer, asked).question == aps.AUTH_QUESTION
        assert f"knowledge_answer:{similar}" not in str(db.get(ApplicationExecutionTask, task.id).plan)
    assert browser.filled == []


def test_the_apply_kit_sees_each_sensitive_answer_it_is_asked_to_confirm(sessions, browser):
    user = aps.owner(sessions)
    ids = aps.standard_answers(sessions, user)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    aps.drive(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        task = db.get(ApplicationExecutionTask, aps.task_of(sessions, job_id).id)
        assert application_execution.confirmations(db, task) == [
            {"id": ids["auth"], "question": aps.AUTH_QUESTION, "answer": "Yes"}]


def test_a_deployment_that_cannot_run_the_worker_shows_it_as_off(sessions, monkeypatch):
    with sessions() as db:
        assert application_execution.available_mode(db) == "assist"
        monkeypatch.setattr(application_execution, "_postgres", lambda _db: False)  # SQLite
        assert application_execution.available_mode(db) == "off"


def test_turning_agent_graphs_off_still_ends_the_runs_left_over(sessions, browser, monkeypatch):
    """AGENT_GRAPHS_ENABLED off unschedules the worker's own job; the always-on applications pass settles its tasks."""
    from app.services import scheduled_jobs as sj

    user = aps.owner(sessions)
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    monkeypatch.setattr(aps.get_settings(), "agent_graphs_enabled", False)
    assert aps.get_settings().application_execution_interval_minutes == 0
    result = sj.applications(aps.ctx(sessions))
    assert result["executions_ended"] == 1
    task = aps.task_of(sessions, job_id)
    assert (task.status, task.result_code) == ("SKIPPED", application_execution.MODE_OFF)


def test_a_run_stopped_while_saving_questions_saves_no_more(sessions, browser, monkeypatch):
    """Each saved question commits and ends the thread lock. A Kill landing between two of them stops the run at the
    next one: the stopped run leaves no further questions behind."""
    from app.services import knowledge, qualification

    user = aps.owner(sessions)  # no saved answers: two required questions to ask
    job_id = aps.approved_job(sessions, user, FORM)
    aps.enqueue(sessions, user, job_id)
    original, saved = knowledge.request_answer, []

    def save_then_kill(db, job, question, **kwargs):
        out = original(db, job, question, **kwargs)
        saved.append(question)
        with sessions() as other:
            other.info["user_id"] = user
            task = other.scalar(select(ApplicationExecutionTask).where(ApplicationExecutionTask.job_id == job_id))
            qualification.stop_threads(other, [task.agent_thread_id])
            other.commit()
        return out

    monkeypatch.setattr(knowledge, "request_answer", save_then_kill)
    aps.drive(sessions)
    assert len(saved) == 1
    with sessions() as db:
        db.info["user_id"] = user
        asked = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.answer == "")).all()
        assert [q.question for q in asked] == saved
