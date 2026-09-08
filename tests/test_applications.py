from datetime import UTC, datetime

import pytest

from app.db import Base, SessionLocal, engine
from app.models import Application, Job, ResumeVariant
from app.services.applications import (
    ApplicationOutcome,
    dispatch_application,
    get_application_settings,
)


class FakeExecutor:
    provider = "fake-tested-adapter"

    def __init__(self, outcome=None, error=None, reconciliation=None):
        self.outcome = outcome or ApplicationOutcome(
            status="submitted",
            provider=self.provider,
            external_reference="fake-123",
            submitted_at=datetime.now(UTC).replace(tzinfo=None),
            evidence={"receipt": "ok"},
        )
        self.error = error
        self.reconciliation = reconciliation
        self.calls = 0
        self.reconcile_calls = 0

    def execute(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.outcome

    def reconcile(self, request, previous):
        self.reconcile_calls += 1
        return self.reconciliation or previous


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def eligible(db):
    policy = get_application_settings(db)
    policy.application_mode = "automatic"
    job = Job(
        source="manual",
        source_ref="policy-job",
        title="Engineer",
        company="Acme",
        location="Remote",
        url="https://example.test/apply",
        description="",
        fit_score=90,
    )
    db.add(job)
    db.commit()
    resume = ResumeVariant(job_id=job.id, version=1, markdown="reviewed", reviewer_score=90)
    db.add(resume)
    db.commit()
    return job, resume, policy


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda j, r, p: setattr(p, "application_mode", "manual_approval"), "manual approval"),
        (lambda j, r, p: setattr(r, "reviewer_score", None), "not been reviewed"),
        (lambda j, r, p: setattr(r, "reviewer_score", 20), "Resume review score"),
        (lambda j, r, p: setattr(j, "fit_score", 20), "Job fit score"),
        (lambda j, r, p: setattr(j, "url", "not-a-url"), "URL is invalid"),
        (
            lambda j, r, p: setattr(
                j, "raw", {"ats_questions": [{"question": "Visa?", "required": True, "answer": ""}]}
            ),
            "ATS questions",
        ),
        (lambda j, r, p: setattr(p, "excluded_companies", ["acme"]), "Company is excluded"),
        (lambda j, r, p: setattr(p, "excluded_locations", ["remote"]), "Location is excluded"),
    ],
)
def test_every_policy_gate_blocks_dispatch(mutate, reason):
    db = SessionLocal()
    job, resume, policy = eligible(db)
    mutate(job, resume, policy)
    db.commit()
    executor = FakeExecutor()
    result = dispatch_application(db, job, executor)
    assert result.status == "blocked"
    assert reason in result.blocked_reason
    assert executor.calls == 0
    db.close()


def test_duplicate_dispatch_returns_stored_submission():
    db = SessionLocal()
    job, _, _ = eligible(db)
    executor = FakeExecutor()
    first = dispatch_application(db, job, executor)
    second = dispatch_application(db, job, executor)
    assert first.status == second.status == "submitted"
    assert first.external_reference == second.external_reference == "fake-123"
    assert executor.calls == 1
    db.close()


def test_timeout_is_ambiguous_and_reconciled_before_retry():
    db = SessionLocal()
    job, _, _ = eligible(db)
    executor = FakeExecutor(
        error=TimeoutError(),
        reconciliation=ApplicationOutcome(
            status="submitted",
            provider="fake-tested-adapter",
            external_reference="found-by-reconciliation",
        ),
    )
    assert dispatch_application(db, job, executor).status == "needs_attention"
    assert dispatch_application(db, job, executor).status == "submitted"
    assert executor.calls == 1
    assert executor.reconcile_calls == 1
    db.close()


def test_ambiguous_response_is_reconciled_before_retry():
    db = SessionLocal()
    job, _, _ = eligible(db)
    executor = FakeExecutor(
        outcome=ApplicationOutcome(
            status="needs_attention",
            provider="fake-tested-adapter",
            ambiguous=True,
            evidence={"response_id": "maybe"},
        ),
        reconciliation=ApplicationOutcome(
            status="failed",
            provider="fake-tested-adapter",
            blocked_reason="confirmed absent",
            recoverable=True,
        ),
    )
    assert dispatch_application(db, job, executor).status == "needs_attention"
    assert dispatch_application(db, job, executor).status == "failed"
    assert executor.calls == executor.reconcile_calls == 1
    db.close()


@pytest.mark.parametrize("recoverable", [True, False])
def test_recoverable_and_permanent_failures(recoverable):
    db = SessionLocal()
    job, _, _ = eligible(db)
    executor = FakeExecutor(
        outcome=ApplicationOutcome(
            status="failed",
            provider="fake-tested-adapter",
            blocked_reason="rejected",
            recoverable=recoverable,
        )
    )
    result = dispatch_application(db, job, executor)
    assert result.status == "failed"
    assert result.recoverable is recoverable
    dispatch_application(db, job, executor)
    assert executor.calls == (2 if recoverable else 1)
    db.close()


def test_mode_switch_does_not_dispatch_queued_work():
    db = SessionLocal()
    job, _, policy = eligible(db)
    queued = Application(job_id=job.id, resume_id=job.resumes[0].id, status="queued")
    db.add(queued)
    db.commit()
    policy.application_mode = "manual_approval"
    db.commit()
    policy.application_mode = "automatic"
    db.commit()
    assert db.get(Application, queued.id).status == "queued"
    db.close()
