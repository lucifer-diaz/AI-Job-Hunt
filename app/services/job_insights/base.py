"""The shapes every Job Insights source shares: the job snapshot it reads, the observations it returns, its status.

A source adapter (`InsightSource`) answers one question about one job (the posting's salary, Glassdoor's interview
reports, the company's own values page...) and returns observations with their provenance. It never raises: a failure
is its own SourceResult, so one source failing never fails the others. It never writes to the database and never sees
a session; the job is a frozen snapshot read once before any network call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.services.job_insights.matching import Target

# Source status, as the Sources panel shows it: ✓ OK, — NO_DATA, ! FAILED, ○ NOT_CONFIGURED.
OK, NO_DATA, FAILED, NOT_CONFIGURED, BLOCKED, NEEDS_REVIEW = (
    "OK", "NO_DATA", "FAILED", "NOT_CONFIGURED", "BLOCKED", "NEEDS_REVIEW")
SOURCE_STATUSES = (OK, NO_DATA, FAILED, NOT_CONFIGURED, BLOCKED, NEEDS_REVIEW)


@dataclass(frozen=True)
class Snapshot:
    """What research needs from a job, read once so no session is held while sources run."""

    task_id: int
    job_id: int
    user_id: int
    application_id: int | None
    title: str
    company: str
    location: str
    target: Target
    website: str | None
    posting_salary: dict[str, Any] | None  # compensation.extract_posting_salary of the board's own fields
    board_salary: dict[str, Any] | None  # Job.salary_* as the hunter stored them, with salary_source
    structured: dict[str, Any]  # the current structured JD ({} when stale or unparsed)
    recruiter: dict[str, Any]  # JobInsight.recruiter_input plus the application's recruiter name
    force: bool
    now: datetime
    attempt: int = 1
    # In memory only, for the Apify adapters; never stored, logged or shown (repr=False keeps it out of tracebacks).
    apify_token: str | None = field(default=None, repr=False)


@dataclass
class ObservationIn:
    """One fact a source found, before it is stored as an InsightObservation."""

    category: str  # COMPENSATION | COMPANY | VALUES | CULTURE | SENTIMENT | INTERVIEW_PROCESS | INTERVIEW_QUESTION
    source_type: str  # EMPLOYER | JOB_DESCRIPTION | RECRUITER | OFFICIAL_COMPANY | GLASSDOOR | PAYSCALE | ...
    data: dict[str, Any]
    confidence: float
    title: str | None = None
    source_url: str | None = None
    source_reference: str | None = None
    provider: str | None = None
    actor_id: str | None = None
    observed_at: datetime | None = None
    match_scope: str | None = None
    match_status: str = "ACCEPTED"
    match_reason: str | None = None
    recency: str | None = None
    cache_key: str | None = None
    fetched_at: datetime | None = None  # set on observations reused from the cache: when they were really fetched


@dataclass
class SourceResult:
    name: str
    status: str
    observations: list[ObservationIn] = field(default_factory=list)
    detail: str | None = None
    cached: bool = False
    rejected: int = 0  # observations dropped as another company, role or country
    review: int = 0  # observations held back as NEEDS_REVIEW

    def summary(self, fetched_at: datetime) -> dict[str, Any]:
        return {"status": self.status, "detail": (self.detail or "")[:300] or None,
                "observations": len(self.observations), "cached": self.cached, "rejected": self.rejected,
                "needs_review": self.review, "fetched_at": fetched_at.isoformat()}


class InsightSource(Protocol):
    name: str
    label: str
    source_type: str
    company_level: bool  # True: the answer depends on the company only (cached per company, not per role)

    def configured(self) -> str | None:
        """None when the source can run; otherwise why not (shown as NOT_CONFIGURED)."""

    def fetch(self, snapshot: Snapshot) -> SourceResult:
        """Research the job. Never raises."""
