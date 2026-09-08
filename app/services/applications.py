"""Provider-neutral, policy-gated application execution.

No concrete job-platform adapter is registered here.  An adapter must implement
``ApplicationExecutor`` and be covered by adapter tests before it is advertised.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationSettings, Job, ResumeVariant

APPLICATION_MODES = ("manual_approval", "automatic")
EXECUTION_STATES = ("queued", "in_progress", "blocked", "submitted", "failed", "needs_attention")


@dataclass(frozen=True)
class ApplicationRequest:
    job: Job
    resume: ResumeVariant
    idempotency_key: str


@dataclass
class ApplicationOutcome:
    status: Literal["blocked", "submitted", "failed", "needs_attention"]
    provider: str
    external_reference: str = ""
    submitted_at: datetime | None = None
    blocked_reason: str = ""
    unresolved_questions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    recoverable: bool | None = None
    ambiguous: bool = False


class ApplicationExecutor(Protocol):
    """Adapter boundary. Implementations must honor ``request.idempotency_key``."""

    provider: str

    def execute(self, request: ApplicationRequest) -> ApplicationOutcome: ...

    def reconcile(
        self, request: ApplicationRequest, previous: ApplicationOutcome
    ) -> ApplicationOutcome: ...


class UnsupportedExecutor:
    provider = "unconfigured"

    def execute(self, request: ApplicationRequest) -> ApplicationOutcome:
        return ApplicationOutcome(
            status="blocked",
            provider=self.provider,
            blocked_reason="No tested application adapter is configured",
        )

    def reconcile(
        self, request: ApplicationRequest, previous: ApplicationOutcome
    ) -> ApplicationOutcome:
        return previous


def get_application_settings(db: Session) -> ApplicationSettings:
    policy = db.get(ApplicationSettings, 1)
    if policy is None:
        policy = ApplicationSettings(id=1)
        db.add(policy)
        db.commit()
        db.refresh(policy)
    if policy.application_mode not in APPLICATION_MODES:  # defend against manually edited databases
        policy.application_mode = "manual_approval"
        db.commit()
    return policy


def make_idempotency_key(job_id: int, resume_id: int, version: int) -> str:
    return hashlib.sha256(
        f"application:{job_id}:resume:{resume_id}:v{version}".encode()
    ).hexdigest()


def _required_unanswered(job: Job) -> list[str]:
    questions = (job.raw or {}).get("ats_questions", [])
    return [
        str(q.get("question") or q.get("id") or "Required ATS question")
        for q in questions
        if q.get("required") and not str(q.get("answer", "")).strip()
    ]


def policy_blockers(
    db: Session, job: Job, resume: ResumeVariant, policy: ApplicationSettings | None = None
) -> tuple[list[str], list[str]]:
    policy = policy or get_application_settings(db)
    reasons: list[str] = []
    questions = _required_unanswered(job)
    if policy.application_mode != "automatic":
        reasons.append("Application mode is manual approval")
    if resume.reviewer_score is None:
        reasons.append("Resume has not been reviewed")
    elif resume.reviewer_score < policy.min_resume_score:
        reasons.append("Resume review score is below the configured threshold")
    if job.fit_score is None or job.fit_score < policy.min_fit_score:
        reasons.append("Job fit score is below the configured threshold")
    parsed = urlparse(job.url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        reasons.append("Application URL is invalid")
    if questions:
        reasons.append("Required ATS questions are unresolved")
    company = job.company.casefold()
    location = job.location.casefold()
    if any(x.strip().casefold() in company for x in policy.excluded_companies if x.strip()):
        reasons.append("Company is excluded by policy")
    if any(x.strip().casefold() in location for x in policy.excluded_locations if x.strip()):
        reasons.append("Location is excluded by policy")
    successful = db.scalar(
        select(Application.id).where(
            Application.job_id == job.id, Application.status == "submitted"
        )
    )
    if successful:
        reasons.append("A successful submission already exists")
    return reasons, questions


def _latest_resume(db: Session, job: Job) -> ResumeVariant | None:
    return db.scalar(
        select(ResumeVariant)
        .where(ResumeVariant.job_id == job.id)
        .order_by(desc(ResumeVariant.version))
        .limit(1)
    )


def _persist_outcome(
    db: Session, application: Application, outcome: ApplicationOutcome
) -> ApplicationOutcome:
    application.status = outcome.status
    application.provider = outcome.provider
    application.external_reference = outcome.external_reference
    application.submitted_at = outcome.submitted_at
    application.applied_at = outcome.submitted_at
    application.blocked_reason = outcome.blocked_reason
    application.unresolved_questions = outcome.unresolved_questions
    application.receipt = outcome.evidence
    application.needs_reconciliation = outcome.ambiguous
    application.failure_recoverable = outcome.recoverable
    if outcome.status == "submitted":
        application.job.status = "applied"
    db.commit()
    return outcome


def dispatch_application(
    db: Session, job: Job, executor: ApplicationExecutor
) -> ApplicationOutcome:
    """Apply policy and execute at most once; ambiguous attempts reconcile before retry."""
    resume = _latest_resume(db, job)
    if resume is None:
        return ApplicationOutcome(
            status="blocked",
            provider=executor.provider,
            blocked_reason="No approved resume version exists",
        )
    key = make_idempotency_key(job.id, resume.id, resume.version)
    application = db.scalar(select(Application).where(Application.idempotency_key == key))
    if application and application.status == "submitted":
        return ApplicationOutcome(
            status="submitted",
            provider=application.provider,
            external_reference=application.external_reference,
            submitted_at=application.submitted_at,
            evidence=application.receipt,
        )
    if application and application.status == "failed" and application.failure_recoverable is False:
        return ApplicationOutcome(
            status="failed",
            provider=application.provider,
            external_reference=application.external_reference,
            blocked_reason=application.blocked_reason,
            evidence=application.receipt,
            recoverable=False,
        )

    if application is None:
        application = job.application or Application(job_id=job.id)
        application.resume_id = resume.id
        application.idempotency_key = key
        db.add(application)

    request = ApplicationRequest(job=job, resume=resume, idempotency_key=key)
    if application.needs_reconciliation:
        previous = ApplicationOutcome(
            status="needs_attention",
            provider=application.provider,
            external_reference=application.external_reference,
            evidence=application.receipt,
            ambiguous=True,
        )
        try:
            return _persist_outcome(db, application, executor.reconcile(request, previous))
        except TimeoutError:
            return _persist_outcome(
                db,
                application,
                ApplicationOutcome(
                    status="needs_attention",
                    provider=executor.provider,
                    blocked_reason="Reconciliation timed out; retry is suppressed",
                    ambiguous=True,
                ),
            )

    reasons, questions = policy_blockers(db, job, resume)
    if reasons:
        return _persist_outcome(
            db,
            application,
            ApplicationOutcome(
                status="blocked",
                provider=executor.provider,
                blocked_reason="; ".join(reasons),
                unresolved_questions=questions,
            ),
        )

    application.status = "in_progress"
    application.provider = executor.provider
    db.commit()
    try:
        outcome = executor.execute(request)
    except TimeoutError:
        outcome = ApplicationOutcome(
            status="needs_attention",
            provider=executor.provider,
            blocked_reason="Executor timed out; reconcile before retry",
            ambiguous=True,
        )
    except Exception as exc:
        outcome = ApplicationOutcome(
            status="failed", provider=executor.provider, blocked_reason=str(exc), recoverable=True
        )
    if outcome.ambiguous and outcome.status != "needs_attention":
        outcome.status = "needs_attention"
    return _persist_outcome(db, application, outcome)
