"""Safe, PostgreSQL-backed work claiming for workflow workers.

A worker claims jobs in some states, does the work, then either completes the claim (a validated
state transition) or fails it (bounded retry, then FAILED).

Locking has two layers:

* ``SELECT ... FOR UPDATE SKIP LOCKED`` inside the claim transaction, so concurrent claimers never
  pick the same row and never wait on each other.
* A lease written to the row (``locked_by``, ``lock_token``, ``lock_expires_at``) that survives the
  claim transaction. Other workers ignore a leased job until the lease expires, which is how a crashed
  worker's job is recovered.

Every later write (complete, fail, advance, heartbeat, release) re-locks the row and checks the lease
token, so a worker whose lease expired and was taken over can never apply a stale result.

Attempts are counted when a job is claimed, so a job that crashes its worker still uses up attempts.
All functions take ``now`` so tests can move the clock; production callers leave it unset.
"""
from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Job, WorkflowTransition
from app.services.lifecycle import (
    DEFAULT_RETRYABLE,
    TERMINAL_STATES,
    TRANSITIONS,
    ErrorCategory,
    InvalidTransition,
    WorkflowState,
    parse_state,
    parse_states,
    validate_transition,
)
from app.settings import get_settings

MAX_ERROR_LENGTH = 4000


class LeaseLost(RuntimeError):
    """The claim is no longer held: its lease expired and another worker took the job, or it was released."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: int = 60
    max_delay_seconds: int = 3600

    def __post_init__(self):
        if self.max_attempts < 1 or self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("Retry policy needs max_attempts >= 1 and 0 <= base delay <= max delay")

    @classmethod
    def from_settings(cls) -> RetryPolicy:
        s = get_settings()
        return cls(s.workflow_max_attempts, s.workflow_retry_base_seconds, s.workflow_retry_max_seconds)

    def delay(self, attempt: int) -> timedelta:
        """Exponential backoff after the given (1-based) attempt, capped at max_delay_seconds."""
        seconds = self.base_delay_seconds * 2 ** max(0, attempt - 1)
        return timedelta(seconds=min(self.max_delay_seconds, seconds))


@dataclass(frozen=True)
class Claim:
    job_id: int
    token: str
    worker_id: str
    state: WorkflowState
    attempt: int
    lease_expires_at: datetime


def transition(db: Session, job: Job, target: object, *, actor: str = "system", reason: str = "",
               now: datetime | None = None) -> WorkflowTransition:
    """Validate and apply one state change and record it in workflow_transitions. Does not commit."""
    current = job.state
    target_state = validate_transition(current, target, previous_state=job.previous_state)
    job.state = target_state
    job.previous_state = current
    job.updated_at = now or utcnow()
    record = WorkflowTransition(job_id=job.id, from_state=current, to_state=target_state, actor=actor[:200],
                                reason=reason[:MAX_ERROR_LENGTH], attempt_count=job.attempt_count,
                                error_category=job.error_category, created_at=job.updated_at)
    db.add(record)
    return record


def _require_postgres(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Workflow work claiming requires PostgreSQL (FOR UPDATE SKIP LOCKED)")


def _clear_lease(job: Job) -> None:
    job.locked_at = job.locked_by = job.lock_token = job.lock_expires_at = None


def claim(db: Session, states: Iterable[object], *, worker_id: str, limit: int = 1, lease_seconds: int | None = None,
          policy: RetryPolicy | None = None, now: datetime | None = None) -> list[Claim]:
    """Lease up to ``limit`` jobs in ``states`` that are due and not held by a live lease. Commits."""
    _require_postgres(db)
    targets = parse_states(states)
    if not targets:
        raise ValueError("Name at least one state to claim")
    terminal = [s for s in targets if s in TERMINAL_STATES]
    if terminal:
        raise ValueError(f"Terminal states cannot be claimed: {', '.join(terminal)}")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    policy = policy or RetryPolicy.from_settings()
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds if lease_seconds is not None else get_settings().workflow_lease_seconds)

    rows = db.scalars(
        select(Job)
        .where(Job.state.in_(targets),
               or_(Job.lock_expires_at.is_(None), Job.lock_expires_at <= now),
               or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
               or_(Job.retryable.is_(None), Job.retryable.is_(True)),
               Job.attempt_count < policy.max_attempts)
        .order_by(Job.next_attempt_at.asc().nulls_first(), Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Job)
        .execution_options(populate_existing=True)
    ).all()

    claims = []
    for job in rows:
        if job.lock_token is not None:
            # The previous holder never finished: keep a diagnosable trace of the crash.
            job.last_error = (f"Lease held by {job.locked_by} expired at {job.lock_expires_at.isoformat()} "
                              f"before attempt {job.attempt_count} finished")
            job.error_category = ErrorCategory.LEASE_EXPIRED
        job.locked_at, job.locked_by, job.lock_token = now, worker_id[:200], uuid.uuid4().hex
        job.lock_expires_at = now + lease
        job.attempt_count += 1
        job.last_attempt_at, job.next_attempt_at, job.updated_at = now, None, now
        claims.append(Claim(job.id, job.lock_token, job.locked_by, job.state, job.attempt_count, job.lock_expires_at))
    db.commit()
    return claims


def _owned(db: Session, claimed: Claim) -> Job:
    """Re-lock the claimed row and prove the claim still holds it."""
    job = db.scalars(select(Job).where(Job.id == claimed.job_id).with_for_update()
                     .execution_options(populate_existing=True)).one_or_none()
    if job is None or job.lock_token is None or job.lock_token != claimed.token:
        db.rollback()
        holder = f"now held by {job.locked_by}" if job is not None and job.locked_by else "no longer leased"
        raise LeaseLost(f"Job {claimed.job_id}: claim by {claimed.worker_id} (attempt {claimed.attempt}) is {holder}")
    return job


def _give_up(db: Session, job: Job, *, actor: str, now: datetime) -> None:
    """Stop retrying. Pre-submission work becomes FAILED; post-submission states keep their place, parked."""
    job.retryable, job.next_attempt_at = False, None
    if WorkflowState.FAILED in TRANSITIONS[job.state]:
        transition(db, job, WorkflowState.FAILED, actor=actor, now=now,
                   reason=f"{job.error_category} after {job.attempt_count} attempt(s): {job.last_error or ''}")


def complete(db: Session, claimed: Claim, target: object, *, reason: str = "", now: datetime | None = None) -> Job:
    """Finish the claimed work with a validated transition, release the lease and reset retry state. Commits."""
    target_state = parse_state(target)
    if target_state is WorkflowState.FAILED:
        raise ValueError("Use fail() to record a failure so the error is diagnosable")
    now = now or utcnow()
    job = _owned(db, claimed)
    try:
        transition(db, job, target_state, actor=claimed.worker_id, reason=reason, now=now)
    except InvalidTransition:
        db.rollback()
        raise
    _clear_lease(job)
    job.attempt_count = 0
    job.last_error = job.error_category = job.retryable = job.next_attempt_at = None
    db.commit()
    return job


def advance(db: Session, claimed: Claim, target: object, *, reason: str = "", now: datetime | None = None) -> Claim:
    """Transition while keeping the lease, e.g. QUALIFIED → TAILORING before the long-running work. Commits."""
    now = now or utcnow()
    job = _owned(db, claimed)
    try:
        transition(db, job, target, actor=claimed.worker_id, reason=reason, now=now)
    except InvalidTransition:
        db.rollback()
        raise
    db.commit()
    return replace(claimed, state=job.state)


def heartbeat(db: Session, claimed: Claim, *, lease_seconds: int | None = None, now: datetime | None = None) -> Claim:
    """Extend a live lease for long-running work. Commits."""
    now = now or utcnow()
    job = _owned(db, claimed)
    job.lock_expires_at = now + timedelta(seconds=lease_seconds or get_settings().workflow_lease_seconds)
    job.updated_at = now
    db.commit()
    return replace(claimed, lease_expires_at=job.lock_expires_at)


def release(db: Session, claimed: Claim, *, now: datetime | None = None) -> Job:
    """Hand the job back untouched (e.g. graceful shutdown). The attempt is not counted. Commits."""
    job = _owned(db, claimed)
    _clear_lease(job)
    job.attempt_count = max(0, job.attempt_count - 1)
    job.updated_at = now or utcnow()
    db.commit()
    return job


def fail(db: Session, claimed: Claim, category: object, message: str, *, retryable: bool | None = None,
         policy: RetryPolicy | None = None, now: datetime | None = None) -> Job:
    """Record a failed attempt. Retries with backoff while attempts remain, otherwise gives up. Commits."""
    category = ErrorCategory(category)
    retryable = DEFAULT_RETRYABLE[category] if retryable is None else retryable
    policy = policy or RetryPolicy.from_settings()
    now = now or utcnow()
    job = _owned(db, claimed)
    job.last_error = (message or str(category))[:MAX_ERROR_LENGTH]
    job.error_category = category
    job.updated_at = now
    _clear_lease(job)
    if retryable and job.attempt_count < policy.max_attempts:
        job.retryable = True
        job.next_attempt_at = now + policy.delay(job.attempt_count)
    else:
        _give_up(db, job, actor=claimed.worker_id, now=now)
    db.commit()
    return job


def recover_expired_leases(db: Session, *, policy: RetryPolicy | None = None, now: datetime | None = None,
                           limit: int = 100) -> int:
    """Crash recovery sweep: release leases whose workers died, scheduling a retry or giving up. Commits."""
    _require_postgres(db)
    policy = policy or RetryPolicy.from_settings()
    now = now or utcnow()
    jobs = db.scalars(select(Job).where(Job.lock_token.is_not(None), Job.lock_expires_at <= now)
                      .order_by(Job.lock_expires_at).limit(limit).with_for_update(skip_locked=True)
                      .execution_options(populate_existing=True)).all()
    for job in jobs:
        job.last_error = (f"Lease held by {job.locked_by} expired at {job.lock_expires_at.isoformat()} "
                          f"before attempt {job.attempt_count} finished")
        job.error_category = ErrorCategory.LEASE_EXPIRED
        job.updated_at = now
        _clear_lease(job)
        if job.attempt_count < policy.max_attempts:
            job.retryable = True
            job.next_attempt_at = now + policy.delay(job.attempt_count)
        else:
            _give_up(db, job, actor="lease-recovery", now=now)
    db.commit()
    return len(jobs)


def requeue_failed(db: Session, job_id: int, *, actor: str, reason: str = "", now: datetime | None = None) -> Job:
    """Operator action: return a FAILED job to the state it failed in with a fresh attempt budget. Commits."""
    now = now or utcnow()
    job = db.scalars(select(Job).where(Job.id == job_id).with_for_update()
                     .execution_options(populate_existing=True)).one_or_none()
    if job is None:
        raise LookupError(f"Job {job_id} not found")
    if job.state is not WorkflowState.FAILED:
        db.rollback()
        raise InvalidTransition(job.state, job.previous_state or job.state, f"Job {job_id} is {job.state}, not FAILED")
    try:
        transition(db, job, job.previous_state, actor=actor, reason=reason or "Requeued after failure", now=now)
    except InvalidTransition:
        db.rollback()
        raise
    job.attempt_count = 0
    job.retryable = job.next_attempt_at = None
    db.commit()
    return job
