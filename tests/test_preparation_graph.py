"""The resume preparation graph (Phase 4), on PostgreSQL (TEST_DATABASE_URL; skipped otherwise).

Each test drives the real passes (the preparation scheduler job and the tailor worker) with the mock model, and checks
what the job, its versions, its tasks and the source resume look like afterwards. The Reviewer's answer is scripted
where a test needs a decision; tailoring itself is the real backend tailor (mock provider).
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.agents import checkpoint, runtime, threads
from app.agents import preparation as graph
from app.models import (
    AgentEffect,
    ApprovedPacket,
    Job,
    JobInsightTask,
    JobPreparationTask,
    TailorTask,
    WorkflowTransition,
)
from app.services import preparation, workers, workflow
from app.services.lifecycle import STOPPED_BY_USER
from tests import preparation_support as ps
from tests import qualification_support as qs


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch):
    ps.configure(monkeypatch, "on")
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    from app.services import backoff

    backoff.reset_cache()
    return qs.app_sessions(pg_engine)


def _job(sessions, job_id):
    with sessions() as db:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job


def _approve(sessions, user_id, job_id):
    with sessions() as db:
        db.info["user_id"] = user_id
        return workflow.approve_job(db, db.get(Job, job_id)).id


# ───────────── the main path ─────────────

def test_a_pass_waits_for_approval_and_ends_approved(sessions, monkeypatch):
    user = ps.owner(sessions)
    before = ps.resumes(sessions, user)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    task = ps.task_of(sessions, job_id)
    assert (task.status, task.waiting_for, task.step) == ("PENDING", "APPROVAL", graph.AWAIT_APPROVAL)
    assert ps.counts(sessions, job_id) == {"tailor": 1, "variants": 1, "rankings": 0}
    assert _job(sessions, job_id).status == "tailored"
    (variant,) = ps.variants(sessions, job_id)
    assert variant.reviewer_notes["decision"] == "PASS" and variant.reviewer_notes["source"] == "preparation_graph"

    _approve(sessions, user, job_id)
    ps.drive(sessions)
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.outcome) == ("COMPLETED", graph.APPROVED)
    assert ps.thread_of(sessions, task).result["approved_packet_id"]
    job = _job(sessions, job_id)
    assert job.status == "approved" and job.state.value == "READY_TO_APPLY"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(JobInsightTask).where(JobInsightTask.job_id == job_id)) == 1
    # The source resume was read, never written; the tailored version names it.
    assert ps.resumes(sessions, user) == before
    assert variant.content["context"]["id"] == before[0][0]


def test_a_retailor_gets_one_more_pass_with_the_review_to_fix(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    reviewed = ps.script_reviews(monkeypatch, ["RETAILOR", "PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    first, second = ps.variants(sessions, job_id)
    assert reviewed == [1, 2]
    assert first.reviewer_notes["decision"] == "RETAILOR" and second.reviewer_notes["decision"] == "PASS"
    with sessions() as db:
        tasks = db.scalars(select(TailorTask).where(TailorTask.job_id == job_id).order_by(TailorTask.id)).all()
        assert [t.options.get("revise_from") for t in tasks] == [None, first.id]
        assert all(t.options.get("conducted") for t in tasks)
    task = ps.task_of(sessions, job_id)
    assert task.waiting_for == "APPROVAL" and task.revision == 1


def test_a_failed_review_gets_one_review_only_pass_not_a_second_tailoring(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    scripted, calls = workflow.review_resume, []

    def flaky(db, resume):
        calls.append(resume.version)
        if len(calls) == 1:
            raise RuntimeError("reviewer timed out")
        return scripted(db, resume)

    monkeypatch.setattr(workflow, "review_resume", flaky)
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    assert calls == [1, 1]  # the same version, reviewed again
    with sessions() as db:
        tasks = db.scalars(select(TailorTask).where(TailorTask.job_id == job_id).order_by(TailorTask.id)).all()
        assert [(t.mode, t.status) for t in tasks] == [("tailor", "failed"), ("review", "succeeded")]
        assert tasks[1].options.get("conducted")
    (variant,) = ps.variants(sessions, job_id)
    assert variant.reviewer_notes["decision"] == "PASS"
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.waiting_for, task.step) == ("PENDING", "APPROVAL", graph.AWAIT_APPROVAL)


def test_a_version_a_person_reviewed_while_the_run_waited_is_decided_not_failed(sessions, monkeypatch):
    import asyncio

    from app.services import scheduled_jobs, tailor_tasks

    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    scripted, calls = workflow.review_resume, []

    def flaky(db, resume):
        calls.append(resume.version)
        if len(calls) == 1:
            raise RuntimeError("reviewer timed out")
        return scripted(db, resume)

    monkeypatch.setattr(workflow, "review_resume", flaky)
    ps.dispatch(sessions, user, job_id)
    asyncio.run(scheduled_jobs.preparation_job(ps.ctx(sessions)))  # the run queues its pass and waits
    tailor_tasks.run_pending(sessions, worker_id="test-tailor")  # the pass saves version 1; its review fails
    with sessions() as db:  # before the run resumes, the person sends the packet to review (Tailor screen)
        db.info["user_id"] = user
        assert tailor_tasks.enqueue(db, [job_id], mode="review")["queued"]
    tailor_tasks.run_pending(sessions, worker_id="test-tailor")
    ps.drive(sessions)

    assert calls == [1, 1]  # the person's review is used: the graph queues no review of its own
    with sessions() as db:
        modes = db.scalars(select(TailorTask.mode).where(TailorTask.job_id == job_id).order_by(TailorTask.id)).all()
        assert modes == ["tailor", "review"]
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.waiting_for, task.step) == ("PENDING", "APPROVAL", graph.AWAIT_APPROVAL)


def test_a_review_that_fails_again_ends_the_run_without_a_third_pass(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.clean_letters(monkeypatch)

    def down(db, resume):
        raise RuntimeError("reviewer timed out")

    monkeypatch.setattr(workflow, "review_resume", down)
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    with sessions() as db:
        modes = db.scalars(select(TailorTask.mode).where(TailorTask.job_id == job_id).order_by(TailorTask.id)).all()
        assert modes == ["tailor", "review"]
    task = ps.task_of(sessions, job_id)
    assert task.outcome == graph.FAILED and ps.thread_of(sessions, task).result["code"] == "REVIEW_FAILED"


def test_the_revision_loop_stops_at_its_limit_and_waits_for_a_person(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["RETAILOR"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    assert ps.counts(sessions, job_id)["tailor"] == 3  # the first pass and max_revisions (2) more, never a fourth
    job = _job(sessions, job_id)
    assert job.status == "pending_approval"  # a person decides; it never passed by running out of tries
    task = ps.task_of(sessions, job_id)
    assert task.waiting_for == "APPROVAL" and task.revision == 2


def test_an_invented_claim_overrules_a_reviewer_pass(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.inject_claim(monkeypatch)
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    rows = ps.variants(sessions, job_id)
    assert len(rows) == 3
    for row in rows:
        assert row.reviewer_notes["decision"] == "RETAILOR"  # the evidence checks beat the reviewer's PASS
        assert row.reviewer_notes["recommendation"] == "RETAILOR"
        codes = {i["code"] for i in row.reviewer_notes["deterministic_issues"]}
        assert "UNKNOWN_EVIDENCE" in codes
    assert _job(sessions, job_id).status == "pending_approval"
    # The person can still look at it, but nothing with an invented claim was ever marked as passed.
    assert all(r.reviewer_notes["decision"] != "PASS" for r in rows)


# ───────────── never ranks, never runs without what it needs ─────────────

@pytest.mark.strict_ranking
def test_a_job_without_a_completed_analysis_is_never_ranked_or_tailored(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user, ranked=False)
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)

    task = ps.task_of(sessions, job_id)
    assert (task.status, task.outcome, task.result_code) == ("COMPLETED", graph.BLOCKED, "RANKING_REQUIRED")
    assert ps.counts(sessions, job_id) == {"tailor": 0, "variants": 0, "rankings": 0}


# ───────────── a person's decisions ─────────────

def test_sending_a_packet_back_tailors_again_and_rejecting_ends_the_run(sessions, monkeypatch):
    from app.workspace import reject_one, send_back_one

    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        send_back_one(db, db.get(Job, job_id), "Shorter summary, please")
        db.commit()
    ps.drive(sessions)
    assert ps.counts(sessions, job_id)["variants"] == 2
    task = ps.task_of(sessions, job_id)
    assert task.waiting_for == "APPROVAL" and task.revision == 1

    with sessions() as db:
        db.info["user_id"] = user
        reject_one(db, db.get(Job, job_id), "not_interested", "")
        db.commit()
    ps.drive(sessions)
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.outcome) == ("COMPLETED", graph.REJECTED)


def test_a_waiting_run_is_not_claimed_until_a_person_decides(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)
    with sessions() as db:
        assert preparation.claim(db, worker_id="w", limit=10) == []
        efforts = db.scalar(select(func.count()).select_from(AgentEffect))
    ps.drive(sessions)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentEffect)) == efforts  # nothing ran again


# ───────────── kill, restart, retry ─────────────

def test_kill_stops_the_run_its_thread_and_its_tailoring_pass(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    task_id = ps.dispatch(sessions, user, job_id)
    import asyncio

    from app.services import scheduled_jobs as sj

    asyncio.run(sj.preparation_job(ps.ctx(sessions)))  # queues the first tailoring pass and waits
    with sessions() as db:
        db.info["user_id"] = user
        workers.kill(db, user, "preparation", [task_id])
    ps.drive(sessions)
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.last_error) == ("SKIPPED", STOPPED_BY_USER)
    assert ps.thread_of(sessions, task).status == "STOPPED"
    with sessions() as db:
        assert db.scalar(select(TailorTask.status).where(TailorTask.job_id == job_id)) == "cancelled"
        assert preparation.dispatch(db, db.get(Job, job_id)) is None  # no sweep revives it


def test_a_crash_after_queueing_never_queues_a_second_pass(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    crashed = []

    def hold(point):
        if point == "tailor:after_enqueue" and not crashed:
            crashed.append(point)
            raise RuntimeError("worker died")

    original = preparation.process
    monkeypatch.setattr(preparation, "process", lambda s, c, **kw: original(s, c, hold=hold))
    ps.drive(sessions)
    assert crashed
    assert ps.counts(sessions, job_id)["tailor"] == 1  # the retry found the queued pass and waited for it
    task = ps.task_of(sessions, job_id)
    assert task.waiting_for == "APPROVAL" and task.attempt_count >= 1


# ───────────── isolation, API, modes ─────────────

def test_another_account_cannot_resume_or_see_the_run(sessions, monkeypatch):
    user, other = ps.owner(sessions), ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)
    task = ps.task_of(sessions, job_id)
    with sessions() as db:
        db.info["user_id"] = other
        assert db.scalar(select(func.count()).select_from(JobPreparationTask)) == 0
        with pytest.raises((threads.ThreadNotFound, threads.ThreadOwnershipError)):
            runtime.resume(db, task.agent_thread_id, {"action": "continue"}, "w")
    with sessions() as db:
        db.info["user_id"] = user
        with pytest.raises(runtime.ThreadBusy):  # worker-owned: a person resumes it by deciding, not over the API
            runtime.resume(db, task.agent_thread_id, {"action": "continue"}, "w")


def test_mode_off_hands_waiting_runs_back(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)
    ps.set_mode(monkeypatch, "off")
    ps.drive(sessions)
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.result_code) == ("SKIPPED", preparation.ROUTED_TO_LEGACY)
    assert _job(sessions, job_id).status == "tailored"  # the packet stays for the person, as on the current path


def test_shadow_compares_the_decision_and_writes_nothing(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.set_mode(monkeypatch, "off")
    from app.services import review_results, tailor_tasks

    with sessions() as db:  # the current path: the tailor worker, then the decision Claude Desktop would make
        db.info["user_id"] = user
        tailor_tasks.enqueue(db, [job_id])
    tailor_tasks.run_pending(sessions, worker_id="legacy")
    with sessions() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        variant = review_results.latest_variant(db, job_id)
        review_results.apply_review(db, job, variant, review_results.review_of(job_id, variant), source="test")
    before = ps.variants(sessions, job_id)
    ps.set_mode(monkeypatch, "shadow")
    ps.drive(sessions)
    shadow = ps.task_of(sessions, job_id, mode="SHADOW")
    assert shadow.status == "COMPLETED" and shadow.parity["match"] is True
    assert shadow.parity["fields"]["decision"] == {"predicted": "PASS", "actual": "PASS", "match": True}
    after = ps.variants(sessions, job_id)
    assert [(v.id, v.reviewer_notes) for v in after] == [(v.id, v.reviewer_notes) for v in before]
    assert ps.counts(sessions, job_id)["tailor"] == 1


def test_approval_records_the_real_stages_and_the_packet(sessions, monkeypatch):
    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)
    _approve(sessions, user, job_id)
    with sessions() as db:
        history = [(t.from_state.value if t.from_state else None, t.to_state.value, t.actor) for t in db.scalars(
            select(WorkflowTransition).where(WorkflowTransition.job_id == job_id).order_by(WorkflowTransition.id))]
        packet = db.scalar(select(ApprovedPacket).where(ApprovedPacket.job_id == job_id))
    assert history == [("QUALIFIED", "TAILORING", "reconcile"), ("TAILORING", "TAILORED", "reconcile"),
                       ("TAILORED", "REVIEWED", "reconcile"), ("REVIEWED", "READY_TO_APPLY", "approval")]
    assert packet is not None and packet.revoked_at is None and len(packet.digest) == 64


def test_mode_off_gives_an_unstarted_job_back_to_automatic_tailoring(sessions, monkeypatch):
    from sqlalchemy import update

    from app.services import tailor_tasks

    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    with sessions() as db:  # as auto_tailor.queue leaves it: the claim taken, the job handed to the graph
        db.info["user_id"] = user
        db.execute(update(Job).where(Job.id == job_id).values(auto_tailor_status=tailor_tasks.AUTO_QUEUED))
        preparation.dispatch(db, db.get(Job, job_id), reason="auto")
        db.commit()
    ps.set_mode(monkeypatch, "off")  # before the graph queued any tailoring pass
    ps.drive(sessions)
    assert ps.task_of(sessions, job_id).result_code == preparation.ROUTED_TO_LEGACY
    job = _job(sessions, job_id)
    assert job.auto_tailor_status is None, "the claim is given back, so automatic tailoring can pick the job up"
    with sessions() as db:
        db.info["user_id"] = user
        held = db.scalar(select(func.count()).select_from(Job).where(Job.id == job_id, preparation.untouched()))
    assert held == 1, "a handed-back run no longer holds the job from automatic tailoring"
    assert ps.counts(sessions, job_id)["tailor"] == 0


def test_automatic_approval_of_a_pass_does_not_depend_on_a_fresh_decision(sessions, monkeypatch):
    """A replayed DECIDE (its effect already recorded) still approves a PASS that is still waiting; a packet a person
    already decided, or an older version, is never approved there."""
    from app.agents import effects
    from app.services.user_settings import settings_for

    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)  # manual mode: the PASS waits for a person
    assert _job(sessions, job_id).status == "tailored"
    monkeypatch.setattr(effects, "held", lambda db, run: None)
    with sessions() as db:
        db.info["user_id"] = user
        setting = settings_for(db, user)
        setting.application_mode, setting.min_review_score = "automatic", 50
        db.commit()
        job = db.get(Job, job_id)
        latest = graph._latest_variant(db, job_id)
        graph._maybe_auto_approve(db, None, job, latest, graph.PASS)
    assert _job(sessions, job_id).status == "approved"
    with sessions() as db:  # again, as a second replay would: nothing more happens
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        graph._maybe_auto_approve(db, None, job, graph._latest_variant(db, job_id), graph.PASS)
        packets = db.scalar(select(func.count()).select_from(ApprovedPacket).where(ApprovedPacket.job_id == job_id))
    assert packets == 1


def test_a_waiting_run_ends_when_the_application_is_set_to_rejected(sessions, monkeypatch):
    from app.main import application_update
    from app.models import Application
    from app.schemas import ApplicationUpdateIn

    user = ps.owner(sessions)
    job_id = ps.qualified_job(sessions, user)
    ps.script_reviews(monkeypatch, ["PASS"])
    ps.dispatch(sessions, user, job_id)
    ps.drive(sessions)
    with sessions() as db:
        db.info["user_id"] = user
        job = db.get(Job, job_id)
        if job.application is None:
            db.add(Application(job_id=job_id, status="pending_approval"))
            db.commit()
        application_id = db.get(Job, job_id).application.id
    with sessions() as db:  # PATCH /api/applications/{id}, with the account's session
        db.info["user_id"] = user
        application_update(application_id, ApplicationUpdateIn(status="rejected"), db)
    ps.drive(sessions)
    task = ps.task_of(sessions, job_id)
    assert (task.status, task.outcome) == ("COMPLETED", graph.REJECTED)


def test_a_shadow_run_with_nothing_to_compare_is_not_a_match():
    assert preparation.parity({"predicted_decision": "PASS", "actual_decision": "UNKNOWN"})["match"] is None
    assert preparation.parity({"predicted_decision": None, "actual_decision": "PASS"})["match"] is None
    assert preparation.parity({"predicted_decision": "PASS", "actual_decision": "PASS"})["match"] is True
    assert preparation.parity({"predicted_decision": "PASS", "actual_decision": "RETAILOR"})["match"] is False
