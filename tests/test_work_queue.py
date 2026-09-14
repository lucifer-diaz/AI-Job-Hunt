"""Claiming, locking, retries and crash recovery against a real PostgreSQL database.

Needs TEST_DATABASE_URL (see tests/conftest.py); skipped otherwise. Time is passed explicitly, so
lease expiry and backoff are tested without sleeping.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Job, WorkflowTransition
from app.services import work_queue as wq
from app.services.lifecycle import TERMINAL_STATES, ErrorCategory, InvalidState, InvalidTransition
from app.services.lifecycle import WorkflowState as S

T0 = datetime(2026, 1, 1, 12, 0, 0)
POLICY = wq.RetryPolicy(max_attempts=3, base_delay_seconds=60, max_delay_seconds=600)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def db(pg_sessions):
    session = pg_sessions()
    yield session
    session.close()


def add_jobs(db: Session, count: int = 1, state: S = S.DISCOVERED) -> list[int]:
    jobs = [Job(source="test", source_ref=uuid.uuid4().hex, title=f"Role {i}", company="Acme", state=state)
            for i in range(count)]
    db.add_all(jobs)
    db.commit()
    return [job.id for job in jobs]


def claim(db: Session, states=(S.DISCOVERED,), worker="w1", now=T0, limit=1, policy=POLICY, lease=60):
    return wq.claim(db, states, worker_id=worker, now=now, limit=limit, policy=policy, lease_seconds=lease)


def fresh(db: Session, job_id: int) -> Job:
    db.expire_all()
    return db.get(Job, job_id)


def history(db: Session, job_id: int) -> list[tuple]:
    rows = db.scalars(select(WorkflowTransition).where(WorkflowTransition.job_id == job_id)
                      .order_by(WorkflowTransition.id)).all()
    return [(row.from_state, row.to_state) for row in rows]


# ───────────── state integrity ─────────────

def test_arbitrary_state_strings_are_rejected_by_orm_and_database(db):
    job_id, = add_jobs(db)
    job = db.get(Job, job_id)
    for bad in ("BOGUS", "discovered", "shortlisted"):
        with pytest.raises(InvalidState):
            job.state = bad
    db.rollback()
    for column, value in (("state", "BOGUS"), ("previous_state", "NOPE"), ("error_category", "OOPS")):
        with pytest.raises(IntegrityError):
            db.execute(text(f"UPDATE jobs SET {column} = :value WHERE id = :id"), {"value": value, "id": job_id})
            db.commit()
        db.rollback()
    assert fresh(db, job_id).state is S.DISCOVERED


def test_direct_assignment_cannot_skip_the_lifecycle(db):
    job_id, = add_jobs(db)
    job = db.get(Job, job_id)
    with pytest.raises(InvalidTransition):
        job.state = S.TAILORED
    job.state = S.NORMALIZED
    db.commit()
    assert fresh(db, job_id).state is S.NORMALIZED


def test_database_rejects_half_written_leases(db):
    job_id, = add_jobs(db)
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE jobs SET locked_by = 'ghost' WHERE id = :id"), {"id": job_id})
        db.commit()
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE jobs SET attempt_count = -1 WHERE id = :id"), {"id": job_id})
        db.commit()


def test_new_jobs_start_discovered_with_clean_workflow_fields(db):
    job = fresh(db, add_jobs(db)[0])
    assert job.state is S.DISCOVERED and job.previous_state is None and job.attempt_count == 0
    assert job.created_at and job.updated_at
    assert (job.locked_at, job.locked_by, job.lock_token, job.next_attempt_at, job.last_error) == (None,) * 5


# ───────────── valid and invalid transitions ─────────────

def test_full_lifecycle_through_claims_records_history(db):
    job_id, = add_jobs(db)
    path = [S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANKED, S.QUALIFIED, S.TAILORING, S.TAILORED, S.REVIEWED,
            S.READY_TO_APPLY, S.APPLYING, S.APPLIED, S.FOLLOWUP_DUE, S.FOLLOWED_UP, S.INTERVIEW, S.OFFER]
    for step, (current, target) in enumerate(zip(path, path[1:], strict=False)):
        claimed, = claim(db, [current], now=at(step * 100))
        job = wq.complete(db, claimed, target, reason=f"step {step}", now=at(step * 100 + 1))
        assert job.state is target and job.previous_state is current
        assert job.attempt_count == 0 and job.lock_token is None
    assert history(db, job_id) == list(zip(path, path[1:], strict=False))
    rows = fresh(db, job_id).transitions
    assert {row.actor for row in rows} == {"w1"} and rows[0].reason == "step 0"


def test_invalid_completion_is_rejected_and_the_job_is_untouched(db):
    job_id, = add_jobs(db)
    claimed, = claim(db)
    with pytest.raises(InvalidTransition):
        wq.complete(db, claimed, S.TAILORED)
    with pytest.raises(InvalidState):
        wq.complete(db, claimed, "WHATEVER")
    with pytest.raises(ValueError, match="fail"):
        wq.complete(db, claimed, S.FAILED)
    job = fresh(db, job_id)
    assert job.state is S.DISCOVERED and job.lock_token == claimed.token and history(db, job_id) == []
    wq.complete(db, claimed, S.NORMALIZED, now=at(1))
    assert history(db, job_id) == [(S.DISCOVERED, S.NORMALIZED)]


def test_advance_changes_state_but_keeps_the_lease(db):
    job_id, = add_jobs(db, state=S.QUALIFIED)
    claimed, = claim(db, [S.QUALIFIED])
    tailoring = wq.advance(db, claimed, S.TAILORING, now=at(1))
    job = fresh(db, job_id)
    assert tailoring.state is S.TAILORING and job.state is S.TAILORING and job.lock_token == claimed.token
    assert claim(db, [S.TAILORING], worker="w2", now=at(2)) == []
    assert wq.complete(db, tailoring, S.TAILORED, now=at(3)).state is S.TAILORED


# ───────────── locking ─────────────

def test_claim_writes_a_lease_and_hides_the_job_from_other_workers(db):
    job_id, = add_jobs(db)
    claimed, = claim(db, worker="w1", lease=60)
    job = fresh(db, job_id)
    assert (job.locked_by, job.lock_token, job.locked_at, job.lock_expires_at) == ("w1", claimed.token, T0, at(60))
    assert job.attempt_count == claimed.attempt == 1 and job.last_attempt_at == T0
    assert claim(db, worker="w2", now=at(59)) == []


def test_claim_respects_limit_state_and_order(db):
    ids = add_jobs(db, 5)
    add_jobs(db, 2, state=S.RANKED)
    assert [c.job_id for c in claim(db, limit=3)] == ids[:3]
    assert [c.job_id for c in claim(db, limit=10, worker="w2")] == ids[3:]


def test_claim_argument_validation(db):
    with pytest.raises(ValueError):
        claim(db, states=[])
    with pytest.raises(InvalidState):
        claim(db, states=["BOGUS"])
    with pytest.raises(ValueError):
        claim(db, limit=0)


def test_claiming_requires_postgresql():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, pytest.raises(RuntimeError, match="PostgreSQL"):
        wq.claim(session, [S.DISCOVERED], worker_id="w1")


def test_skip_locked_never_waits_for_a_row_locked_by_another_transaction(db, pg_sessions):
    first, second = add_jobs(db, 2)
    holder, other = pg_sessions(), pg_sessions()
    try:
        holder.execute(select(Job).where(Job.id == first).with_for_update())  # row lock held, not committed
        other.execute(text("SET lock_timeout = '3s'"))  # a blocking claim would error instead of hanging
        started = time.monotonic()
        claims = claim(other, worker="w2", limit=10)
        assert [c.job_id for c in claims] == [second]
        assert time.monotonic() - started < 2
    finally:
        holder.rollback()
        holder.close()
        other.close()


def test_concurrent_workers_never_claim_the_same_job(db, pg_sessions):
    ids = add_jobs(db, 60)
    workers = 8
    barrier = threading.Barrier(workers)

    def run(n: int) -> list[int]:
        session = pg_sessions()
        got: list[int] = []
        try:
            barrier.wait()
            while batch := claim(session, worker=f"w{n}", limit=4):
                got += [c.job_id for c in batch]
        finally:
            session.close()
        return got

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(run, range(workers)))
    claimed = [job_id for result in results for job_id in result]
    assert len(claimed) == len(set(claimed)) == len(ids)
    assert sorted(claimed) == ids
    rows = db.scalars(select(Job)).all()
    assert all(job.attempt_count == 1 and job.lock_token for job in rows)


def test_many_workers_racing_for_one_job_get_exactly_one_claim(db, pg_sessions):
    job_id, = add_jobs(db)
    workers = 12
    barrier = threading.Barrier(workers)

    def run(n: int) -> list:
        session = pg_sessions()
        try:
            barrier.wait()
            return claim(session, worker=f"w{n}")
        finally:
            session.close()

    with ThreadPoolExecutor(workers) as pool:
        results = [c for batch in pool.map(run, range(workers)) for c in batch]
    assert len(results) == 1 and results[0].job_id == job_id
    assert fresh(db, job_id).attempt_count == 1


# ───────────── retries ─────────────

def test_retry_policy_backoff_is_exponential_and_capped():
    policy = wq.RetryPolicy(max_attempts=5, base_delay_seconds=60, max_delay_seconds=300)
    assert [policy.delay(n).total_seconds() for n in range(1, 6)] == [60, 120, 240, 300, 300]
    with pytest.raises(ValueError):
        wq.RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        wq.RetryPolicy(base_delay_seconds=100, max_delay_seconds=10)


def test_retry_count_backoff_then_failed_with_diagnostics(db):
    job_id, = add_jobs(db)
    c1, = claim(db)
    job = wq.fail(db, c1, ErrorCategory.EXTERNAL_SERVICE, "Apify returned 503", policy=POLICY, now=T0)
    assert job.state is S.DISCOVERED and job.attempt_count == 1 and job.retryable is True
    assert job.next_attempt_at == at(60) and job.last_error == "Apify returned 503"
    assert job.error_category is ErrorCategory.EXTERNAL_SERVICE and job.lock_token is None

    assert claim(db, now=at(59)) == []
    c2, = claim(db, now=at(60))
    assert c2.attempt == 2
    job = wq.fail(db, c2, ErrorCategory.TIMEOUT, "timed out", policy=POLICY, now=at(60))
    assert job.attempt_count == 2 and job.next_attempt_at == at(180)

    assert claim(db, now=at(179)) == []
    c3, = claim(db, now=at(180))
    job = wq.fail(db, c3, ErrorCategory.TIMEOUT, "timed out again", policy=POLICY, now=at(180))
    assert job.state is S.FAILED and job.previous_state is S.DISCOVERED
    assert job.attempt_count == 3 and job.retryable is False and job.next_attempt_at is None
    assert job.error_category is ErrorCategory.TIMEOUT and job.last_error == "timed out again"
    assert claim(db, now=at(86400)) == []

    last = fresh(db, job_id).transitions[-1]
    assert (last.from_state, last.to_state, last.attempt_count) == (S.DISCOVERED, S.FAILED, 3)
    assert last.error_category is ErrorCategory.TIMEOUT and "TIMEOUT after 3 attempt(s)" in last.reason


def test_non_retryable_errors_fail_on_the_first_attempt(db):
    validation, blocked, overridden = add_jobs(db, 3, state=S.APPLYING)
    claims = {c.job_id: c for c in claim(db, [S.APPLYING], limit=3)}
    job = wq.fail(db, claims[validation], ErrorCategory.VALIDATION, "unknown screening question", policy=POLICY)
    assert (job.state, job.attempt_count, job.retryable) == (S.FAILED, 1, False)
    job = wq.fail(db, claims[blocked], ErrorCategory.BLOCKED, "CAPTCHA shown", policy=POLICY)
    assert job.state is S.FAILED and job.error_category is ErrorCategory.BLOCKED
    job = wq.fail(db, claims[overridden], ErrorCategory.EXTERNAL_SERVICE, "portal says closed",
                  retryable=False, policy=POLICY)
    assert job.state is S.FAILED


def test_retryable_override_retries_a_normally_fatal_category(db):
    job_id, = add_jobs(db)
    claimed, = claim(db)
    job = wq.fail(db, claimed, ErrorCategory.VALIDATION, "dataset not ready", retryable=True, policy=POLICY, now=T0)
    assert job.state is S.DISCOVERED and job.next_attempt_at == at(60)


def test_success_after_a_retry_clears_retry_state(db):
    job_id, = add_jobs(db)
    c1, = claim(db)
    wq.fail(db, c1, ErrorCategory.RATE_LIMITED, "429", policy=POLICY, now=T0)
    c2, = claim(db, now=at(60))
    job = wq.complete(db, c2, S.NORMALIZED, now=at(61))
    assert (job.attempt_count, job.last_error, job.error_category, job.retryable, job.next_attempt_at) == \
        (0, None, None, None, None)


def test_post_submission_failures_park_instead_of_erasing_the_application(db):
    job_id, = add_jobs(db, state=S.FOLLOWUP_DUE)
    claimed, = claim(db, [S.FOLLOWUP_DUE])
    job = wq.fail(db, claimed, ErrorCategory.CONFIGURATION, "email provider not configured", policy=POLICY)
    assert job.state is S.FOLLOWUP_DUE and job.retryable is False and job.last_error
    assert history(db, job_id) == []
    assert claim(db, [S.FOLLOWUP_DUE], now=at(86400)) == []


# ───────────── terminal states ─────────────

def test_terminal_states_cannot_be_claimed_or_left(db):
    for state in TERMINAL_STATES:
        with pytest.raises(ValueError, match="Terminal"):
            claim(db, [state])
    ids = add_jobs(db, 1, state=S.OFFER) + add_jobs(db, 1, state=S.SKIPPED) + add_jobs(db, 1, state=S.FAILED)
    for job_id in ids:
        job = db.get(Job, job_id)
        with pytest.raises(InvalidTransition):
            job.state = S.DISCOVERED
    db.rollback()


def test_failed_job_can_be_requeued_only_to_where_it_failed(db):
    job_id, other = add_jobs(db, 2, state=S.TAILORING)
    claims = {c.job_id: c for c in claim(db, [S.TAILORING], limit=2)}
    wq.fail(db, claims[job_id], ErrorCategory.VALIDATION, "evidence missing", policy=POLICY, now=T0)
    job = fresh(db, job_id)
    with pytest.raises(InvalidTransition):
        job.state = S.QUALIFIED
    db.rollback()

    job = wq.requeue_failed(db, job_id, actor="operator", now=at(10))
    assert (job.state, job.previous_state, job.attempt_count, job.retryable) == (S.TAILORING, S.FAILED, 0, None)
    assert [c.job_id for c in claim(db, [S.TAILORING], now=at(11))] == [job_id]
    with pytest.raises(InvalidTransition, match="not FAILED"):
        wq.requeue_failed(db, other, actor="operator")
    assert history(db, job_id)[-2:] == [(S.TAILORING, S.FAILED), (S.FAILED, S.TAILORING)]


# ───────────── crash recovery ─────────────

def test_crashed_workers_job_is_reclaimed_after_its_lease_expires(db):
    job_id, = add_jobs(db)
    crashed, = claim(db, worker="crashy", lease=60)
    # The worker process dies here: no complete, fail or release.
    assert claim(db, worker="w2", now=at(59)) == []
    recovered, = claim(db, worker="w2", now=at(60))
    assert recovered.attempt == 2 and recovered.token != crashed.token
    job = fresh(db, job_id)
    assert job.locked_by == "w2" and job.error_category is ErrorCategory.LEASE_EXPIRED and "crashy" in job.last_error
    job = wq.complete(db, recovered, S.NORMALIZED, now=at(61))
    assert job.state is S.NORMALIZED and job.last_error is None


def test_recovery_sweep_schedules_retries_or_fails_exhausted_jobs(db):
    policy = wq.RetryPolicy(max_attempts=2, base_delay_seconds=60, max_delay_seconds=600)
    retry_id, exhausted_id = add_jobs(db, 2)
    db.execute(update(Job).where(Job.id == exhausted_id).values(attempt_count=1))
    db.commit()
    claims = claim(db, limit=2, policy=policy, lease=60)
    assert sorted(c.attempt for c in claims) == [1, 2]

    assert wq.recover_expired_leases(db, policy=policy, now=at(30)) == 0
    assert wq.recover_expired_leases(db, policy=policy, now=at(61)) == 2

    retry = fresh(db, retry_id)
    assert retry.state is S.DISCOVERED and retry.lock_token is None and retry.retryable is True
    assert retry.next_attempt_at == at(121) and retry.error_category is ErrorCategory.LEASE_EXPIRED
    exhausted = fresh(db, exhausted_id)
    assert exhausted.state is S.FAILED and exhausted.retryable is False and exhausted.attempt_count == 2
    assert exhausted.error_category is ErrorCategory.LEASE_EXPIRED
    assert "LEASE_EXPIRED" in exhausted.transitions[-1].reason
    assert wq.recover_expired_leases(db, policy=policy, now=at(1000)) == 0


# ───────────── duplicate worker execution ─────────────

def test_stale_worker_cannot_overwrite_the_worker_that_took_over(db):
    job_id, = add_jobs(db)
    slow, = claim(db, worker="slow", lease=60)
    fast, = claim(db, worker="fast", now=at(61))
    wq.complete(db, fast, S.NORMALIZED, now=at(62))
    for late in (lambda: wq.complete(db, slow, S.NORMALIZED, now=at(63)),
                 lambda: wq.fail(db, slow, ErrorCategory.TRANSIENT, "late", policy=POLICY, now=at(63)),
                 lambda: wq.advance(db, slow, S.SKIPPED, now=at(63)),
                 lambda: wq.heartbeat(db, slow, now=at(63)),
                 lambda: wq.release(db, slow, now=at(63))):
        with pytest.raises(wq.LeaseLost):
            late()
    job = fresh(db, job_id)
    assert job.state is S.NORMALIZED and job.last_error is None
    assert history(db, job_id) == [(S.DISCOVERED, S.NORMALIZED)]


def test_duplicate_delivery_of_the_same_claim_is_applied_once(db, pg_sessions):
    job_id, = add_jobs(db)
    claimed, = claim(db)
    workers = 6
    barrier = threading.Barrier(workers)

    def finish(_: int) -> str:
        session = pg_sessions()
        try:
            barrier.wait()
            wq.complete(session, claimed, S.NORMALIZED, now=at(1))
            return "done"
        except wq.LeaseLost:
            return "lost"
        finally:
            session.close()

    with ThreadPoolExecutor(workers) as pool:
        outcomes = list(pool.map(finish, range(workers)))
    assert outcomes.count("done") == 1 and outcomes.count("lost") == workers - 1
    assert history(db, job_id) == [(S.DISCOVERED, S.NORMALIZED)]


def test_heartbeat_extends_a_live_lease(db):
    job_id, = add_jobs(db)
    claimed, = claim(db, lease=60)
    extended = wq.heartbeat(db, claimed, lease_seconds=60, now=at(50))
    assert extended.lease_expires_at == at(110)
    assert claim(db, worker="w2", now=at(90)) == []
    assert wq.complete(db, extended, S.NORMALIZED, now=at(100)).state is S.NORMALIZED


def test_release_hands_the_job_back_without_using_an_attempt(db):
    job_id, = add_jobs(db)
    claimed, = claim(db)
    job = wq.release(db, claimed, now=at(1))
    assert job.lock_token is None and job.attempt_count == 0
    assert [c.job_id for c in claim(db, worker="w2", now=at(2))] == [job_id]
    with pytest.raises(wq.LeaseLost):
        wq.release(db, claimed)
