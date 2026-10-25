"""The application execution worker's queue (Phase 5): enqueue, claim, run, settle, wait for a person, kill, retry.

    POST /api/jobs/{id}/application-execution → enqueue (refused unless the packet is ready to apply to: the shared
                                                apply_queue rules, the approved digest captured on the task)
    the `application_execution` scheduler job → claim (SKIP LOCKED + guarded UPDATE) → process: start or resume the
                                                task's graph thread (app.agents.application) and settle the task
    a person answers or confirms              → wake: the waiting task becomes claimable again

APPLICATION_EXECUTION_MODE (off by default) chooses what a run may do: `observe` reads the form and resolves where
each answer comes from; `assist` also fills it in an ephemeral browser page and validates it, ending READY_TO_SUBMIT,
and a person submits through the Apply kit, which takes the pre-submit claim (apply_queue.begin_submission).
`guarded_auto` (Phase 6) also lets a run of a consenting account, on an allowlisted route, click the final Submit —
only through app.services.submission, holding the same claim and a SubmissionIntent. The submit stages (SUBMITTING,
CONFIRMING, CONFIRMED) are refused here (`set_stage`) unless that intent is passed, and by the column otherwise.

PostgreSQL is the only queue. Waiting for a person uses no attempt and no polling: a task waiting at NEEDS_INPUT is
not claimable until a person answers or confirms (or asks to Run now). Kill stops the task and its thread; nothing
revives it until a person asks (Retry and Run now queue a new task).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES,
    APPLICATION_EXECUTION_STAGES,
    AgentThread,
    ApplicationExecutionAttempt,
    ApplicationExecutionTask,
    Job,
    KnowledgeAnswer,
)
from app.services import backoff, worker_settings
from app.services.lifecycle import STOPPED_BY_USER
from app.services.redaction import redact_text
from app.settings import get_settings

log = logging.getLogger("jobhunt.application_execution")
SessionFactory = Callable[[], Session]
KIND = "application"
JOB = "application_execution"
PENDING, PROCESSING, COMPLETED, FAILED, SKIPPED = "PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"
OUTCOMES = ("completed", "waiting", "retrying", "failed", "stopped", "busy", "lost")
MODE_OFF = "MODE_OFF"
ERROR_LIMIT = 300
# Reached only by a guarded run holding its SubmissionIntent (app.services.submission); refused otherwise.
SUBMIT_STAGES = frozenset({"SUBMITTING", "CONFIRMING", "CONFIRMED"})
# The intent status each submit stage needs.
STAGE_INTENT = {"SUBMITTING": ("CLICKING",), "CONFIRMING": ("CLICKING",), "CONFIRMED": ("CONFIRMED",)}
SERVER_MODES = {"OBSERVE": ("observe", "assist", "guarded_auto"), "ASSIST": ("assist", "guarded_auto"),
                "GUARDED_AUTO": ("guarded_auto",)}
WAITING = "NEEDS_INPUT"
# Stages a PENDING task waits in for a person: answers (NEEDS_INPUT) or the takeover window (NEEDS_HUMAN, Phase 6).
WAITING_STAGES = ("NEEDS_INPUT", "NEEDS_HUMAN")


class ExecutionRefused(ValueError):
    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)


class SubmitForbidden(RuntimeError):
    """Something tried to move an execution into a submit stage without the SubmissionIntent that authorizes it."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── mode ─────────────

def mode() -> str:
    return get_settings().application_execution_mode_value


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def routes(db: Session) -> bool:
    return mode() != "off" and get_settings().agent_graphs_enabled and _postgres(db)


def available_mode(db: Session) -> str:
    """The mode screens show: the server's mode only where a run can actually be queued (`routes`), else off, so a
    deployment that cannot run the worker (SQLite) never offers an action that is always refused."""
    return mode() if routes(db) else "off"


def set_stage(task: ApplicationExecutionTask, stage: str, *, intent: Any = None) -> None:
    """The only way an execution's stage changes. Refuses unknown names, and the submit stages (SubmitForbidden)
    unless `intent` is this execution's SubmissionIntent in the status that stage needs."""
    if stage not in APPLICATION_EXECUTION_STAGES:
        raise ValueError(f"unknown execution stage {stage}")
    if stage in SUBMIT_STAGES:
        if intent is None or intent.execution_task_id != task.id or intent.status not in STAGE_INTENT[stage] \
                or task.mode != "GUARDED_AUTO":
            raise SubmitForbidden(f"{stage} needs this guarded execution's SubmissionIntent "
                                  "(app.services.submission); nothing else may reach it")
        task._submit_stage_allowed = stage  # read once by the column guard below
    task.stage = stage


@event.listens_for(ApplicationExecutionTask.stage, "set", retval=True)
def _no_submit_stage(target: Any, value: str, _old: Any, _initiator: Any) -> str:
    """Belt and braces: whatever code sets the column, a submit stage reaches the database only through set_stage
    with the execution's intent."""
    if value in SUBMIT_STAGES:
        allowed = target.__dict__.pop("_submit_stage_allowed", None)
        if allowed != value:
            raise SubmitForbidden(f"{value} needs this guarded execution's SubmissionIntent "
                                  "(app.services.submission); nothing else may reach it")
    return value


# ───────────── enqueue ─────────────

def active_task(db: Session, job_id: int) -> ApplicationExecutionTask | None:
    q = ApplicationExecutionTask
    return db.scalar(select(q).where(q.job_id == job_id, q.status.in_(ACTIVE_APPLICATION_EXECUTION_TASK_STATUSES))
                     .limit(1))


def latest(db: Session, job_id: int) -> ApplicationExecutionTask | None:
    q = ApplicationExecutionTask
    return db.scalar(select(q).where(q.job_id == job_id).order_by(q.id.desc()).limit(1))


def enqueue(db: Session, job: Job, *, execution_mode: str | None = None, reason: str = "user_request",
            now: datetime | None = None) -> ApplicationExecutionTask:
    """Queue the execution worker for an approved job, or return its active task. Refused (ExecutionRefused) while
    the mode is off, when the packet may not be used (the shared apply_queue rules), or while a submission of the
    posting is in progress. Captures the approved digest. Flushes in a savepoint; the caller commits."""
    from app.services import apply_queue, approval

    server = mode()
    wanted = (execution_mode or ("ASSIST" if server == "guarded_auto" else server)).upper()
    if not routes(db):
        raise ExecutionRefused("The application worker is off (APPLICATION_EXECUTION_MODE, AGENT_GRAPHS_ENABLED and "
                               "PostgreSQL are needed)", "OFF")
    if server not in SERVER_MODES.get(wanted, ()):
        raise ExecutionRefused(f"This server runs the application worker in {server} mode", "MODE")
    existing = active_task(db, job.id)
    if existing is not None:
        return existing
    reason_not_ready = apply_queue.not_ready_reason(db, job)
    if reason_not_ready:
        raise ExecutionRefused(reason_not_ready, "NOT_READY")
    claim = apply_queue.open_claim(db, job)
    if claim is not None:
        raise ExecutionRefused("A submission of this posting is in progress or its outcome is unknown", "CLAIMED")
    if wanted == "GUARDED_AUTO":
        guarded_refusal(db, job)
    packet = approval.live(db, job.application)
    now = now or utcnow()
    q = ApplicationExecutionTask
    task = q(user_id=job.user_id, job_id=job.id, application_id=job.application.id,
             approved_packet_id=packet.id if packet else None, packet_digest=packet.digest if packet else None,
             mode=wanted, status=PENDING, stage="QUEUED", reason=reason[:40], attempt_count=0, created_at=now,
             updated_at=now)
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:  # queued at the same moment by another request: one task
        found = active_task(db, job.id)
        if found is None:
            raise
        return found
    _nudge_after_commit(db)
    return task


def guarded_refusal(db: Session, job: Job) -> None:
    """Why this job may not run in GUARDED_AUTO (ExecutionRefused), checked when it is queued; the PreSubmitValidator
    checks all of it again before the click."""
    from app.services import ats_map, submission

    if not submission.consent(db, job.user_id)["enabled"]:
        raise ExecutionRefused("Guarded submission is off for this account (Settings → Guarded submission)",
                               "NO_CONSENT")
    match = ats_map.for_job(job)
    if match is None or not match.guarded_capable:
        raise ExecutionRefused("Guarded submission needs a route with a deterministic form adapter (Greenhouse)",
                               "ROUTE_NOT_GUARDED")
    if not match.allowed_by(get_settings().guarded_routes):
        raise ExecutionRefused(f"The route {match.route} is not in APPLICATION_GUARDED_ROUTES", "ROUTE_NOT_ALLOWED")


def _nudge_after_commit(db: Session) -> None:
    if db.info.get("application_execution_nudge"):
        return
    db.info["application_execution_nudge"] = True

    def wake(session: Session) -> None:
        session.info.pop("application_execution_nudge", None)
        from app import scheduler_hooks

        scheduler_hooks.nudge(JOB)

    event.listen(db, "after_commit", wake, once=True)


# ───────────── a person's answers ─────────────

def _ready_to_continue(db: Session, task: ApplicationExecutionTask) -> bool:
    waiting = task.interrupt or {}
    if waiting.get("kind") == "NEEDS_HUMAN":
        return bool(waiting.get("continued"))  # the person said they finished in the window
    needed = list(waiting.get("question_ids") or [])
    if needed:
        rows = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.id.in_(needed))).all()
        if len(rows) != len(set(needed)) or not all(r.verified and (r.answer or "").strip() for r in rows):
            return False
    confirm = set(waiting.get("confirm_question_ids") or [])
    return confirm <= set(task.confirmed_question_ids or [])


def wake(db: Session, task: ApplicationExecutionTask) -> bool:
    """A waiting task whose questions are now answered and confirmed becomes claimable (stage ANSWERING). Does not
    commit."""
    if task.status != PENDING or task.stage not in WAITING_STAGES or not _ready_to_continue(db, task):
        return False
    set_stage(task, "ANSWERING" if task.stage == WAITING else "ROUTED")
    task.next_attempt_at = None
    _nudge_after_commit(db)
    return True


def wake_waiting(db: Session, user_id: int) -> int:
    """After a person answers a question in the Knowledge bank: wake the account's tasks that waited for it. Never
    raises (a savepoint); does not commit."""
    woken = 0
    try:
        with db.begin_nested():
            q = ApplicationExecutionTask
            for task in db.scalars(select(q).where(q.user_id == user_id, q.status == PENDING, q.stage == WAITING)
                                   .limit(200)).all():
                woken += wake(db, task)
            db.flush()
    except Exception:
        log.warning("could not wake waiting application executions", exc_info=True)
    return woken


def continue_after_takeover(db: Session, task_id: int) -> ApplicationExecutionTask:
    """The person finished in the kept window (logged in, entered the code, signed up): the run resumes and re-observes
    the page, revalidating everything before it acts. Commits."""
    task = db.get(ApplicationExecutionTask, task_id)
    if task is None:
        raise ExecutionRefused("Execution not found", "NOT_FOUND")
    waiting = dict(task.interrupt or {})
    if task.status != PENDING or task.stage != "NEEDS_HUMAN" or not waiting.get("takeover"):
        raise ExecutionRefused("This execution is not waiting for you in its browser window", "NOT_WAITING")
    waiting["continued"] = True
    task.interrupt = waiting
    wake(db, task)
    db.commit()
    return task


def end_expired_takeovers(db: Session, now: datetime | None = None, limit: int = 200) -> int:
    """Takeovers nobody came back to: the window is closed (SidecarManager.reap) and the run ends NEEDS_HUMAN
    (TAKEOVER_EXPIRED) with the shared blocker, as a Phase 5 run would; a person retries when ready. Commits."""
    from app.services import apply_queue, qualification

    now = now or utcnow()
    q = ApplicationExecutionTask
    ended = 0
    for task in db.scalars(select(q).where(q.status == PENDING, q.stage == "NEEDS_HUMAN").limit(limit)).all():
        waiting = task.interrupt or {}
        try:
            expires = datetime.fromisoformat(waiting.get("expires_at") or "")
        except ValueError:
            continue
        if waiting.get("continued") or expires > now:
            continue
        task.status, task.completed_at, task.outcome, task.result_code = COMPLETED, now, "NEEDS_HUMAN", \
            "TAKEOVER_EXPIRED"
        qualification.stop_threads(db, [task.agent_thread_id], now, note="Nobody took over the browser window")
        job = db.get(Job, task.job_id)
        reason = {"ACCOUNT_REQUIRED": "account_required", "CAPTCHA_OR_ANTI_BOT": "captcha"}.get(
            waiting.get("reason") or "", "login_required")
        if job is not None and job.application is not None and job.application.status == "approved":
            db.commit()
            apply_queue.record_blocker(db, job, reason, recorded_by="application_worker",
                                       detail=f"The browser window waited for you and closed: {waiting.get('reason')}")
        ended += 1
    db.commit()
    return ended


def confirmations(db: Session, task: ApplicationExecutionTask) -> list[dict[str, Any]]:
    """The saved sensitive answers this execution waits for the person to confirm: each question and the answer that
    would be filled, so a person sees exactly what they authorize. For the account's own Apply kit only (it shows
    saved answers already); never stored on the task, the graph or a log."""
    ids = list((task.interrupt or {}).get("confirm_question_ids") or []) if task.stage == WAITING else []
    if not ids:
        return []
    rows = {r.id: r for r in db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.id.in_(ids))).all()}
    return [{"id": i, "question": rows[i].question, "answer": rows[i].answer} for i in ids if i in rows]


def confirm_answers(db: Session, task_id: int, question_ids: list[int]) -> ApplicationExecutionTask:
    """The person confirms sensitive saved answers for this execution only. Commits."""
    task = db.get(ApplicationExecutionTask, task_id)
    if task is None:
        raise ExecutionRefused("Execution not found", "NOT_FOUND")
    asked = set((task.interrupt or {}).get("confirm_question_ids") or [])
    if task.stage != WAITING or not set(question_ids) <= asked:
        raise ExecutionRefused("These answers are not waiting for confirmation in this execution", "NOT_WAITING")
    task.confirmed_question_ids = sorted(set(task.confirmed_question_ids or []) | set(question_ids))
    wake(db, task)
    db.commit()
    return task


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class ExecutionClaim:
    task_id: int
    token: str
    worker_id: str
    user_id: int
    job_id: int
    run_now: bool = False


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[ExecutionClaim]:
    """Lease up to `limit` due PENDING tasks. Run-now first; a task waiting for a person is skipped unless asked to
    run now; paused accounts are held unless asked. Commits."""
    now = now or utcnow()
    lease = lease_seconds or get_settings().application_execution_lease_seconds
    q = ApplicationExecutionTask
    query = (select(q.id, q.user_id, q.job_id, q.run_now_at)
             .where(q.status == PENDING, or_(q.next_attempt_at.is_(None), q.next_attempt_at <= now),
                    or_(q.stage.not_in(WAITING_STAGES), q.run_now_at.is_not(None)))
             .order_by(q.run_now_at.is_(None), q.id).limit(max(1, limit)))
    paused = worker_settings.paused(db, KIND)
    if paused:
        query = query.where(or_(q.run_now_at.is_not(None), q.user_id.not_in(paused)))
    if _postgres(db):
        query = query.with_for_update(skip_locked=True, of=q)
    claims: list[ExecutionClaim] = []
    for row in db.execute(query).all():
        token = str(uuid.uuid4())
        won = db.execute(update(q).where(q.id == row.id, q.status == PENDING)
                         .values(status=PROCESSING, lock_token=token, locked_by=worker_id[:200], locked_at=now,
                                 lock_expires_at=now + timedelta(seconds=lease), run_now_at=None,
                                 attempt_count=q.attempt_count + 1, started_at=now, updated_at=now)
                         .execution_options(synchronize_session=False)).rowcount
        if won:
            claims.append(ExecutionClaim(row.id, token, worker_id, row.user_id, row.job_id,
                                         run_now=row.run_now_at is not None))
    db.commit()
    return claims


def renew(sessions: SessionFactory, lease_seconds: int | None = None) -> Callable[[list[Any]], set[int]]:
    from app.services import scheduled_jobs

    return scheduled_jobs.renew(sessions, ApplicationExecutionTask,
                                lease_seconds or get_settings().application_execution_lease_seconds, token="token")


def _clear_lease(task: ApplicationExecutionTask) -> None:
    task.lock_token = task.locked_by = task.locked_at = task.lock_expires_at = None


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """PROCESSING tasks whose worker died: RETRYING (pending again) or FAILED at the attempt limit. A dead worker
    never moves a task towards a submit: there is no such stage to move to. Commits."""
    from app.services import submission

    now = now or utcnow()
    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.status == PROCESSING, q.lock_expires_at < now).limit(200)).all()
    for task in rows:
        limit = worker_settings.get(db, task.user_id, KIND, "max_attempts")
        _clear_lease(task)
        if task.mode == "GUARDED_AUTO" and submission.reconcile(db, task, "The worker stopped answering") in (
                submission.UNCERTAIN, submission.CONFIRMED):
            # A click may have happened: the outcome is established by a person, never by another attempt.
            task = db.get(ApplicationExecutionTask, task.id, populate_existing=True)
            _clear_lease(task)
            task.status, task.completed_at, task.outcome = COMPLETED, now, "SUBMISSION_UNCERTAIN"
            task.result_code = "SUBMISSION_UNCERTAIN"
            task.last_error = "The worker stopped after the final click started; say whether it was submitted"
            from app.services import qualification

            qualification.stop_threads(db, [task.agent_thread_id], now, note="Submission outcome unknown")
            continue
        if task.attempt_count >= limit:
            task.status, task.completed_at, task.outcome = FAILED, now, "FAILED"
            set_stage(task, "FAILED")
            task.last_error = "The worker stopped answering (lease expired) on its last attempt"
        else:
            task.status, task.next_attempt_at = PENDING, now
            set_stage(task, "RETRYING")
    db.commit()
    return len(rows)


def _held(db: Session, c: ExecutionClaim) -> ApplicationExecutionTask | None:
    q = ApplicationExecutionTask
    query = select(q).where(q.id == c.task_id, q.user_id == c.user_id)
    if _postgres(db):
        query = query.with_for_update()
    task = db.scalar(query.execution_options(populate_existing=True))
    if task is None or task.status != PROCESSING or task.lock_token != c.token:
        return None
    return task


# ───────────── the event loop the browser lives on ─────────────

def loop_runner(loop: asyncio.AbstractEventLoop, timeout: float | None = None) -> Callable[[Coroutine], Any]:
    """Run a browser coroutine from a worker thread on `loop` (the scheduler's), waiting at most `timeout`."""
    limit = timeout or get_settings().application_execution_step_timeout_seconds

    def run(coro: Coroutine) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=limit)
    return run


@contextmanager
def background_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """An event loop on its own thread, for running the worker outside the scheduler (tests, the dry run)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="application-browser-loop", daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


# ───────────── running one task ─────────────

def attempt_note(db: Session, task: ApplicationExecutionTask, **fields: Any) -> None:
    """Write facts onto the task's current attempt row (created on first use). Does not commit."""
    row = db.scalar(select(ApplicationExecutionAttempt).where(ApplicationExecutionAttempt.task_id == task.id,
                                                              ApplicationExecutionAttempt.attempt == task.attempt_count))
    if row is None:
        row = ApplicationExecutionAttempt(user_id=task.user_id, task_id=task.id, attempt=task.attempt_count,
                                          started_at=utcnow())
        db.add(row)
    for name, value in fields.items():
        setattr(row, name, value)


def process(sessions: SessionFactory, c: ExecutionClaim, *, browser: Any = None,
            run_async: Callable[[Coroutine], Any] | None = None, saver: Any | None = None,
            hold: Callable[[str], None] | None = None) -> str:
    """Start or resume the task's graph thread, run it until it ends or waits, and settle the task. Never raises."""
    from app.services import tracing

    try:
        with tracing.trace("application_execution", workflow_type="job", workflow_entity_id=c.job_id,
                           job_id=c.job_id, user_id=c.user_id), backoff.bypass(c.run_now):
            return _process(sessions, c, browser=browser, run_async=run_async, saver=saver, hold=hold)
    except Exception as exc:
        log.warning("application execution task %s failed", c.task_id, exc_info=True)
        return _settle_error(sessions, c, exc)


def _process(sessions: SessionFactory, c: ExecutionClaim, *, browser: Any, run_async: Any, saver: Any,
             hold: Any) -> str:
    from app.agents import application as graph
    from app.agents import runtime
    from app.infrastructure.application_browser import get_application_sessions

    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"
        attempt_note(db, task)
        thread = db.get(AgentThread, task.agent_thread_id) if task.agent_thread_id else None
        if thread is None:
            key = f"application:{task.job_id}:{task.id}:{task.created_at.isoformat()}:a{task.attempt_count}"
            db.commit()
            thread = runtime.start(db, graph.GRAPH, {"job_id": task.job_id, "task_id": task.id, "mode": task.mode},
                                   idempotency_key=key)
            task = _held(db, c)
            if task is None:
                return "lost"
            task.agent_thread_id = thread.id
        db.commit()
        status, thread_pk = thread.status, thread.id
        if status in ("COMPLETED", "FAILED", "STOPPED"):
            return _settle(sessions, c, thread_pk)
        command = None
        if status == "INTERRUPTED":
            try:
                run, command = runtime.resume(db, thread_pk, {"action": "continue"}, c.worker_id, by_worker=True)
            except runtime.ThreadBusy:
                run = None
        else:
            run = runtime.claim(db, thread_pk, c.user_id, ("QUEUED", "RUNNING"), c.worker_id)
        if run is None:
            return _release_busy(db, c, thread_pk)
    runtime.execute(sessions, run, command, saver=saver, hold=hold,
                    extra={"task_id": c.task_id, "browser": browser or get_application_sessions(),
                           "run_async": run_async or _no_loop})
    return _settle(sessions, c, thread_pk)


def _no_loop(coro: Coroutine) -> Any:
    return asyncio.run(coro)


def _release_busy(db: Session, c: ExecutionClaim, thread_pk: int) -> str:
    thread = db.get(AgentThread, thread_pk)
    task = _held(db, c)
    if task is None:
        return "lost"
    _clear_lease(task)
    task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)
    task.next_attempt_at = (thread.lock_expires_at if thread is not None and thread.lock_expires_at else utcnow()) \
        + timedelta(seconds=1)
    db.commit()
    return "busy"


def _settle(sessions: SessionFactory, c: ExecutionClaim, thread_pk: int) -> str:
    """Write how the thread ended onto the task, only while this claim still holds it."""
    now = utcnow()
    with sessions() as db:
        db.info["user_id"] = c.user_id
        task = _held(db, c)
        if task is None:
            return "lost"
        thread = db.get(AgentThread, thread_pk)
        status = thread.status if thread is not None else "STOPPED"
        _clear_lease(task)
        if status == "INTERRUPTED":
            if task.stage not in WAITING_STAGES:
                set_stage(task, WAITING)
            attempt_note(db, task, stage_reached=task.stage, outcome="WAITING", ended_at=now)
            task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)  # waiting uses no attempt
            task.next_attempt_at, task.last_error = None, None
            wake(db, task)  # everything it needs may already have been answered meanwhile
            db.commit()
            return "waiting"
        if status == "COMPLETED":
            result = thread.result or {}
            task.status, task.completed_at, task.last_error = COMPLETED, now, None
            task.outcome = str(result.get("outcome") or "")[:30] or None
            task.result_code = str(result.get("code") or "")[:60] or None
            attempt_note(db, task, stage_reached=task.stage, outcome=task.outcome,
                         form_fingerprint=task.form_fingerprint, ended_at=now)
            db.commit()
            return "completed"
        if status == "STOPPED":
            task.status, task.completed_at = SKIPPED, now
            task.last_error = (thread.last_error if thread is not None else None) or STOPPED_BY_USER
            task.outcome = "STOPPED"
            set_stage(task, "CANCELLED")
            attempt_note(db, task, stage_reached=task.stage, outcome="STOPPED", ended_at=now)
            db.commit()
            return "stopped"
        message = redact_text((thread.last_error if thread is not None else "") or "graph run failed", ERROR_LIMIT)
        return _retry_or_fail(db, task, message, now)


def _retry_or_fail(db: Session, task: ApplicationExecutionTask, message: str, now: datetime) -> str:
    limit = worker_settings.get(db, task.user_id, KIND, "max_attempts")
    task.last_error, task.agent_thread_id = message, None
    attempt_note(db, task, stage_reached=task.stage, outcome="FAILED", error_category="OTHER", ended_at=now)
    if task.attempt_count >= limit:
        task.status, task.completed_at, task.outcome = FAILED, now, "FAILED"
        set_stage(task, "FAILED")
        db.commit()
        return "failed"
    wait = worker_settings.get(db, task.user_id, KIND, "retry_seconds") * max(1, task.attempt_count)
    task.status, task.next_attempt_at = PENDING, now + timedelta(seconds=wait)
    set_stage(task, "RETRYING")
    db.commit()
    return "retrying"


def _settle_error(sessions: SessionFactory, c: ExecutionClaim, exc: Exception) -> str:
    try:
        with sessions() as db:
            db.info["user_id"] = c.user_id
            task = _held(db, c)
            if task is None:
                return "lost"
            _clear_lease(task)
            return _retry_or_fail(db, task, redact_text(f"{type(exc).__name__}: {exc}", ERROR_LIMIT), utcnow())
    except Exception:
        log.warning("could not settle application execution task %s", c.task_id, exc_info=True)
        return "lost"


# ───────────── stopping, retrying, mode off ─────────────

def on_stopped(db: Session, task: ApplicationExecutionTask, now: datetime) -> None:
    """Kill from Running workers: the task is already SKIPPED with STOPPED_BY_USER; stop its thread too. A guarded run
    stopped after its click started stays SUBMISSION_UNCERTAIN (a kill is not a "not submitted"); one stopped before
    gives its claim back."""
    from app.services import qualification, submission

    task.outcome = "STOPPED"
    _close_window(task)
    if task.mode == "GUARDED_AUTO" and submission.reconcile(db, task, "Stopped by you") == submission.UNCERTAIN:
        task = db.get(ApplicationExecutionTask, task.id, populate_existing=True)
        task.outcome = "SUBMISSION_UNCERTAIN"
    else:
        set_stage(task, "CANCELLED")
    if task.agent_thread_id:
        qualification.stop_threads(db, [task.agent_thread_id], now)


def _close_window(task: ApplicationExecutionTask) -> None:
    """A kept browser-use window (takeover) closes with its execution. Never raises."""
    try:
        from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars

        manager = get_sidecars()
        if manager.get(task.user_id, task.id) is not None:
            manager.run_threadsafe(manager.close(task.user_id, task.id), timeout=20)
    except Exception:  # noqa: BLE001  the reaper closes it at the end of its takeover time anyway
        log.debug("could not close the browser window of execution %s", task.id)


def on_job_deleted(db: Session, job_id: int) -> None:
    from app.services import qualification

    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.job_id == job_id)).all()
    qualification.stop_threads(db, [r.agent_thread_id for r in rows], note="The job was deleted")
    for row in rows:
        db.delete(row)


def requeue(db: Session, task: ApplicationExecutionTask, user_id: int) -> ApplicationExecutionTask:
    """Retry and Run now: a new task (and so a new thread) for the job, in the same mode."""
    from app.services.workers import WorkerError

    job = db.scalar(select(Job).where(Job.id == task.job_id, Job.user_id == user_id))
    if job is None:
        raise WorkerError("The job no longer exists", 404)
    try:
        fresh = enqueue(db, job, execution_mode=task.mode, reason="user_request")
    except ExecutionRefused as exc:
        raise WorkerError(str(exc)) from exc
    db.flush()
    return fresh


def release(db: Session, now: datetime | None = None, limit: int = 200) -> int:
    """Mode off with tasks left: end them (MODE_OFF) and stop their threads. Nothing else changes: the Apply kit and
    Claude Desktop keep working as before. Commits."""
    from app.services import qualification

    now = now or utcnow()
    q = ApplicationExecutionTask
    rows = db.scalars(select(q).where(q.status == PENDING).order_by(q.id).limit(limit)).all()
    qualification.stop_threads(db, [r.agent_thread_id for r in rows], now, note="The application worker is off")
    for task in rows:
        task.status, task.completed_at, task.result_code = SKIPPED, now, MODE_OFF
        set_stage(task, "CANCELLED")
    db.commit()
    return len(rows)


# ───────────── screens ─────────────

def view(task: ApplicationExecutionTask) -> dict[str, Any]:
    """The execution for the Apply kit: stage, why it waits, and each field's source and status — never a value."""
    form = task.form or {}
    plan = (task.plan or {}).get("fields") or {}
    fields = [{"id": f["id"], "label": f["label"], "required": f["required"], "kind": f["kind"], "eeo": f.get("eeo"),
               "status": (plan.get(f["id"]) or {}).get("status"),
               "source": (plan.get(f["id"]) or {}).get("source"),
               "question_id": (plan.get(f["id"]) or {}).get("question_id")} for f in form.get("fields") or []]
    return {"id": task.id, "job_id": task.job_id, "mode": task.mode, "status": task.status, "stage": task.stage,
            "outcome": task.outcome, "result_code": task.result_code, "route": task.route, "backend": task.backend,
            "interrupt": task.interrupt, "attempts": task.attempt_count, "last_error": task.last_error,
            "submit_control": (form.get("submit") or {}).get("label") if form else None, "fields": fields,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None}
