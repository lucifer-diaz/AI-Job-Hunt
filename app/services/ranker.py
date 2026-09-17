"""Job Ranker: asynchronous, database-backed ranking of jobs against the candidate who is looking.

Nothing here runs inside Apify ingestion, normalization, job search or the API requests that create jobs.
Those only put work in line; the scheduler's `ranking` job does the ranking:

    APIFY RESULT → NORMALIZE → DEDUPLICATE (app.services.hunter)
    → HARD FILTER (prepare)                          deterministic; SKIPPED with a reason on a clear mismatch
    → RANK_PENDING + JobRankingTask (queue_job)      at most one waiting or running task per job
    → claim: FOR UPDATE SKIP LOCKED + lease          job RANK_PENDING → RANKING
    → load CandidateProfile + active resume snapshot + relevant verified Evidence
    → one model call through the provider-independent LLM layer (stage: rank)
    → validate the structured output → store a new JobRanking version
    → RANKED → QUALIFIED / ARCHIVED                  decided in Python by `qualify`, never by the model

Failure handling:
* No profile, no readable resume, missing AI key or a free-only violation: the task waits (no attempt used)
  and the job goes back to RANK_PENDING. Accounts in that state are not claimed until they are fixed.
* Timeout, rate limit, provider outage, malformed JSON or output that fails validation: bounded retries with
  exponential backoff. When attempts run out the job becomes FAILED (requeueable), never ARCHIVED.
* A crashed worker: its lease expires and `recover_expired_leases` retries or fails the task.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, desc, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (ACTIVE_RANKING_TASK_STATUSES, AppSetting, CandidateProfile, Evidence, Job, JobRanking,
                        JobRankingTask, ResumeDocument, ResumeVersion)
from app.prompts.ranking import (QUALIFICATION_RECOMMENDATIONS, RANKING_PROMPT_VERSION, build_ranking_prompt,
                                 posting_facts, ranking_profile_facts)
from app.services.lifecycle import ErrorCategory, InvalidTransition
from app.services.lifecycle import WorkflowState as S
from app.services.llm import (LLMClient, LLMConfigError, LLMError, LLMRateLimitError, LLMResponseError, LLMResult,
                              LLMTimeoutError)
from app.services.user_settings import ai_ready
from app.services.work_queue import transition
from app.settings import get_settings
from app.tenancy import require_user_id

SessionFactory = Callable[[], Session]
PENDING, PROCESSING, COMPLETED, FAILED, SKIPPED = "PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"
PRE_RANK_STATES = (S.DISCOVERED, S.NORMALIZED, S.FILTERED)
RANK_WAITING_STATES = (S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING, S.RANKING)
# "superseded": the job left ranking (e.g. was deleted) before its task finished.
OUTCOMES = ("qualified", "archived", "retrying", "failed", "waiting", "superseded", "lost")
MAX_ERROR_LENGTH = 4000
MIN_RESUME_WORDS = 15
PLACEHOLDER_TITLES = {"untitled"}
PLACEHOLDER_COMPANIES = {"unknown company"}
YEARLY = {"year", "yearly", "annual", "annually", "per year", "yr"}


class LeaseLost(RuntimeError):
    """The task is no longer held by this worker: its lease expired and was taken over, or it was deleted."""


class RankingSchemaError(ValueError):
    """The model answered with JSON that is not a valid ranking for this job."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _profile(db: Session, user_id: int) -> CandidateProfile | None:
    return db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))


# ───────────── candidate policy ─────────────

def _number(value: Any, default: float | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


@dataclass(frozen=True)
class RankingPolicy:
    """The deterministic rules that turn a ranking into QUALIFIED or ARCHIVED, and the hard filters."""

    minimum_fit_score: float
    minimum_confidence: float
    exclude_companies: frozenset[str] = frozenset()
    exclude_title_keywords: tuple[str, ...] = ()
    remote_only: bool = False
    minimum_salary: float | None = None
    salary_currency: str | None = None
    reject_unknown_salary: bool = False

    def public(self) -> dict[str, Any]:
        return {"minimum_fit_score": self.minimum_fit_score, "minimum_confidence": self.minimum_confidence,
                "remote_only": self.remote_only, "minimum_salary": self.minimum_salary,
                "salary_currency": self.salary_currency, "reject_unknown_salary": self.reject_unknown_salary,
                "exclude_companies": sorted(self.exclude_companies),
                "exclude_title_keywords": list(self.exclude_title_keywords)}


def policy_for(db: Session, user_id: int) -> RankingPolicy:
    """An account's policy: Settings → preferences["ranking"], the search profile, then server defaults."""
    settings = get_settings()
    profile = _profile(db, user_id)
    preferences = (profile.search_preferences if profile else None) or {}
    search = preferences.get("search_profile") or {}
    row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
    ranking = ((row.preferences if row else None) or {}).get("ranking") or {}
    minimum_salary = _number(ranking.get("minimum_salary"), None)
    currency = str(ranking.get("salary_currency") or "").strip().upper() or None
    return RankingPolicy(
        minimum_fit_score=_number(ranking.get("minimum_fit_score"), float(settings.min_fit_score)),
        minimum_confidence=_number(ranking.get("minimum_confidence"), float(settings.min_ranking_confidence)),
        exclude_companies=frozenset(c.strip().lower() for c in preferences.get("exclude_companies") or []
                                    if isinstance(c, str) and c.strip()),
        exclude_title_keywords=tuple(k.strip().lower() for k in search.get("exclude_keywords") or []
                                     if isinstance(k, str) and k.strip()),
        remote_only=bool(search.get("is_remote")),
        minimum_salary=minimum_salary if minimum_salary and minimum_salary > 0 else None,
        salary_currency=currency,
        reject_unknown_salary=bool(ranking.get("reject_unknown_salary")),
    )


# ───────────── deterministic normalization and hard filters ─────────────

def normalization_problem(job: Job) -> str | None:
    title, company = (job.title or "").strip(), (job.company or "").strip()
    if not title or title.lower() in PLACEHOLDER_TITLES or not company or company.lower() in PLACEHOLDER_COMPANIES:
        return "Missing title or company"
    return None


def known_annual_salary(job: Job) -> tuple[float, str | None] | None:
    """The yearly salary the posting states, from the board's fields or salary enrichment. None when unknown."""
    high, currency, period = job.salary_max or job.salary_min, job.salary_currency, job.salary_interval
    if not high:
        posting = (job.compensation or {}).get("posting") or {}
        high, currency, period = posting.get("max") or posting.get("min"), posting.get("currency"), posting.get("period")
    if not high or str(period or "").strip().lower() not in YEARLY:
        return None  # hourly, monthly or unstated periods are never converted by guesswork
    return float(high), str(currency).upper() if currency else None


def salary_filter_reason(job: Job, policy: RankingPolicy) -> str | None:
    """Only a known salary below the minimum, or an unknown one when the policy explicitly requires a salary."""
    if policy.minimum_salary is None:
        return None
    salary = known_annual_salary(job)
    if salary is None:
        # Salary enrichment may still find one: unknown only counts once enrichment has looked.
        if policy.reject_unknown_salary and job.compensation_checked_at is not None:
            return "No salary is listed and your ranking policy requires one"
        return None
    amount, currency = salary
    if policy.salary_currency and currency != policy.salary_currency:
        return None  # a different or unknown currency cannot be compared without guessing
    if amount < policy.minimum_salary:
        return f"Listed salary {amount:,.0f} {currency or ''} a year is below your minimum {policy.minimum_salary:,.0f}"
    return None


def hard_filter_reason(job: Job, policy: RankingPolicy) -> str | None:
    """Obvious mismatches that need no model. Unknown values never filter a job out."""
    if job.status == "dismissed":
        return "Dismissed in triage"
    if (job.company or "").strip().lower() in policy.exclude_companies:
        return f"{job.company} is an excluded company"
    title = (job.title or "").lower()
    keyword = next((k for k in policy.exclude_title_keywords if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", title)),
                   None)
    if keyword:
        return f"The title contains the excluded keyword '{keyword}'"
    if policy.remote_only and (job.remote_type or "").strip().lower() == "onsite":
        return "On-site role, and your search is remote only"
    return salary_filter_reason(job, policy)


def prepare(db: Session, job: Job, policy: RankingPolicy, *, actor: str, now: datetime | None = None) -> str | None:
    """DISCOVERED → NORMALIZED → FILTERED, or SKIPPED with the reason. Returns the reason when skipped."""
    if job.state is S.DISCOVERED:
        problem = normalization_problem(job)
        if problem:
            transition(db, job, S.SKIPPED, actor=actor, reason=problem, now=now)
            return problem
        transition(db, job, S.NORMALIZED, actor=actor, reason="Title and company present", now=now)
    if job.state is S.NORMALIZED:
        reason = hard_filter_reason(job, policy)
        if reason:
            transition(db, job, S.SKIPPED, actor=actor, reason=reason, now=now)
            return reason
        transition(db, job, S.FILTERED, actor=actor, reason="Passed hard filters", now=now)
    return None


# ───────────── queueing ─────────────

RERANK_REASONS = {"user_request": "Re-ranking requested by the user", "profile_changed": "Candidate profile changed",
                  "resume_changed": "Active resume changed", "prompt_changed": "Ranking prompt version changed"}


def enqueue(db: Session, job: Job, *, reason: str = "new", force: bool = False,
            now: datetime | None = None) -> JobRankingTask:
    """The job's waiting or running task, creating one when there is none. Flushes, does not commit."""
    active = db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == job.id,
                                                     JobRankingTask.status.in_(ACTIVE_RANKING_TASK_STATUSES))).first()
    if active is not None:
        if force and not active.force:
            active.force, active.reason = True, reason
        return active
    task = JobRankingTask(user_id=job.user_id, job_id=job.id, status=PENDING, reason=reason, force=force,
                          attempt_count=0, created_at=now or utcnow(),
                          candidate_profile_id=db.scalar(select(CandidateProfile.id)
                                                         .where(CandidateProfile.user_id == job.user_id)))
    db.add(task)
    db.flush()
    return task


def queue_job(db: Session, job: Job, *, policy: RankingPolicy, actor: str, reason: str = "new", force: bool = False,
              now: datetime | None = None) -> str | None:
    """Put one job in line for the ranker. Returns None when queued, otherwise why not. Does not commit."""
    state = job.state
    if state in PRE_RANK_STATES:
        skipped = prepare(db, job, policy, actor=actor, now=now)
        if skipped:
            return skipped
    elif state in (S.RANKED, S.QUALIFIED, S.ARCHIVED):
        try:
            transition(db, job, S.RANK_PENDING, actor=actor, reason=RERANK_REASONS.get(reason, reason), now=now)
        except InvalidTransition:
            return "This job was archived outside ranking, so it cannot be re-ranked"
    elif state is S.FAILED and job.previous_state is S.RANK_PENDING:
        transition(db, job, S.RANK_PENDING, actor=actor, reason="Requeued for ranking after it failed", now=now)
        job.attempt_count, job.retryable, job.next_attempt_at = 0, None, None
    elif state is S.RANKING:
        return "This job is being ranked right now"
    elif state is not S.RANK_PENDING:
        return f"This job is {state.value.replace('_', ' ').lower()}; ranking can no longer change it"
    if job.state is S.FILTERED:
        transition(db, job, S.RANK_PENDING, actor=actor, reason="Waiting for the ranker", now=now)
    enqueue(db, job, reason=reason, force=force, now=now)
    return None


def request_ranking(db: Session, job_ids: Iterable[int], *, force: bool, actor: str, only_unranked: bool = False,
                    reason: str = "user_request") -> dict[str, Any]:
    """Queue ranking for jobs of the account this session belongs to. Never ranks synchronously. Commits."""
    user_id = require_user_id(db)
    wanted = list(dict.fromkeys(job_ids))
    for attempt in (1, 2):
        jobs = {job.id: job for job in db.scalars(select(Job).where(Job.id.in_(wanted)).with_for_update()
                                                   .execution_options(populate_existing=True)).all()}
        policy = policy_for(db, user_id)
        queued, skipped = [], []
        for job_id in wanted:
            job = jobs.get(job_id)
            why = ("not found" if job is None
                   else "already ranked" if only_unranked and job.fit_score is not None
                   and job.state not in RANK_WAITING_STATES
                   else queue_job(db, job, policy=policy, actor=actor, reason=reason if force else "new", force=force))
            (skipped.append({"id": job_id, "reason": why}) if why else queued.append(job_id))
        try:
            db.commit()
            break
        except IntegrityError:  # the sweep queued one of these jobs at the same moment
            db.rollback()
            if attempt == 2:
                raise
    return {"queued": queued, "skipped": skipped, "blocked": readiness(db, user_id), "tasks": task_summaries(db, queued)}


# ───────────── accounts, resume snapshots and evidence ─────────────

def resume_problem(text: str | None) -> str | None:
    """Why a saved resume cannot be ranked against, or None when it is usable."""
    clean = (text or "").strip()
    if not clean:
        return "No resume is saved on your candidate profile"
    readable = sum(ch.isprintable() or ch.isspace() for ch in clean) / len(clean)
    if readable < 0.95 or len(re.findall(r"[^\W\d_]{2,}", clean)) < MIN_RESUME_WORDS:
        return "Your saved resume could not be read: upload it again or paste its text in Settings"
    return None


def readiness(db: Session, user_id: int) -> str | None:
    """Why this account's jobs cannot be ranked right now, or None when they can."""
    profile = _profile(db, user_id)
    if profile is None:
        return "No candidate profile yet: fill in your profile in Settings"
    problem = resume_problem(profile.master_resume)
    if problem:
        return problem
    ready, reason = ai_ready(db, user_id)
    return None if ready else reason


def auto_rank_on(db: Session, user_id: int) -> bool:
    profile = _profile(db, user_id)
    search = ((profile.search_preferences if profile else None) or {}).get("search_profile") or {}
    return bool(search.get("auto_rank", True))


def accounts(db: Session) -> tuple[list[int], list[int], dict[str, int]]:
    """Accounts with waiting tasks: those that can be ranked, those with auto-ranking on, and why others wait."""
    owners = db.scalars(select(JobRankingTask.user_id).where(JobRankingTask.status == PENDING).distinct()).all()
    ready, auto, waiting = [], [], Counter()
    for user_id in sorted(owners):
        problem = readiness(db, user_id)
        if problem:
            waiting[problem] += 1
            continue
        ready.append(user_id)
        if auto_rank_on(db, user_id):
            auto.append(user_id)
        else:
            waiting["Auto-ranking is off (only rankings you request run)"] += 1
    return ready, auto, dict(waiting)


def resume_snapshot(db: Session, profile: CandidateProfile, now: datetime | None = None) -> ResumeVersion:
    """The immutable snapshot of the profile's current resume text, created on first use. Commits when created."""
    content = profile.master_resume or ""
    digest = content_hash(content)
    query = select(ResumeVersion).where(ResumeVersion.candidate_profile_id == profile.id,
                                        ResumeVersion.content_hash == digest)
    found = db.scalar(query)
    if found is not None:
        return found
    document = db.scalars(select(ResumeDocument).where(ResumeDocument.user_id == profile.user_id,
                                                       ResumeDocument.active.is_(True))
                          .order_by(desc(ResumeDocument.id))).first()
    source = document if document is not None and (document.extracted_text or "").strip() == content.strip() else None
    version = (db.scalar(select(func.max(ResumeVersion.version))
                         .where(ResumeVersion.candidate_profile_id == profile.id)) or 0) + 1
    row = ResumeVersion(user_id=profile.user_id, candidate_profile_id=profile.id, version=version, content_hash=digest,
                        content=content, resume_document_id=source.id if source else None, created_at=now or utcnow())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:  # another worker snapshotted the same resume a moment ago
        db.rollback()
        found = db.scalar(query)
        if found is None:
            raise
        return found
    return row


STOPWORDS = {"the", "and", "for", "with", "you", "our", "are", "will", "your", "this", "that", "from", "have", "has",
             "who", "all", "can", "not", "but", "was", "were", "their", "they", "into", "about", "work", "working",
             "team", "teams", "role", "years", "year", "experience", "using", "used", "across", "within", "more"}


def _tokens(text: str) -> set[str]:
    return {word.rstrip(".") for word in re.findall(r"[a-z0-9][a-z0-9+#.]{2,}", (text or "").lower())} - STOPWORDS


def relevant_evidence(db: Session, user_id: int, job: Job, limit: int) -> list[dict[str, Any]]:
    """Verified evidence that shares vocabulary with the posting, most overlapping first, capped at `limit`."""
    wanted = _tokens(" ".join([job.title or "", job.description or "", " ".join(job.skills or [])]))
    scored = []
    for item in db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.verified.is_(True))
                           .order_by(Evidence.id)).all():
        words = _tokens(" ".join([item.claim or "", item.context or "", item.role or "",
                                  " ".join(item.skills or []), " ".join(item.tags or [])]))
        overlap = len(words & wanted)
        if overlap:
            scored.append((overlap, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    chosen = sorted((item for _, item in scored[:max(0, limit)]), key=lambda item: item.id)
    return [{"id": e.id, "company": e.company, "role": e.role, "claim": e.claim, "context": e.context,
             "skills": e.skills or [], "tags": e.tags or []} for e in chosen]


# ───────────── structured output and qualification ─────────────

Score = Annotated[float, Field(ge=0, le=100)]


class RankingOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: int
    overall_fit_score: Score
    confidence: float = Field(ge=0, le=1)
    candidate_fit_score: Score
    resume_fit_score: Score
    seniority_match_score: Score
    skills_match_score: Score
    leadership_match_score: Score
    domain_match_score: Score
    location_match_score: Score
    strong_matches: list[str] = Field(default_factory=list)
    partial_matches: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    fatal_gaps: list[str] = Field(default_factory=list)
    resume_gaps: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[int] = Field(default_factory=list)
    recommended_resume_focus: list[str] = Field(default_factory=list)
    qualification_recommendation: str
    reasoning_summary: str

    @field_validator("qualification_recommendation", mode="before")
    @classmethod
    def _recommendation(cls, value: Any) -> Any:
        clean = value.strip().upper() if isinstance(value, str) else value
        if clean not in QUALIFICATION_RECOMMENDATIONS:
            raise ValueError(f"must be one of {', '.join(QUALIFICATION_RECOMMENDATIONS)}")
        return clean

    @field_validator("strong_matches", "partial_matches", "missing_requirements", "fatal_gaps", "resume_gaps",
                     "recommended_resume_focus")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]


def validate_output(data: Any, job_id: int, verified_ids: set[int]) -> tuple[RankingOutput, list[int]]:
    """Validate a model answer for one job. Evidence ids the model was not given are dropped and returned."""
    try:
        output = RankingOutput.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in error['loc']) or 'output'}: {error['msg']}"
                             for error in exc.errors()[:5])
        raise RankingSchemaError(f"Ranking output failed validation: {problems}") from exc
    if output.job_id != job_id:
        raise RankingSchemaError(f"Ranking output is for job {output.job_id}, not job {job_id}")
    cited = list(dict.fromkeys(output.relevant_evidence_ids))
    output.relevant_evidence_ids = [i for i in cited if i in verified_ids]
    return output, [i for i in cited if i not in verified_ids]


@dataclass(frozen=True)
class Decision:
    state: S
    reasons: list[str]


def qualify(result: Any, policy: RankingPolicy) -> Decision:
    """The final call. The model's qualification_recommendation is advice and never decides on its own."""
    score, confidence, fatal = float(result.overall_fit_score), float(result.confidence), list(result.fatal_gaps or [])
    reasons = []
    if score < policy.minimum_fit_score:
        reasons.append(f"Fit score {score:.0f} is below your minimum of {policy.minimum_fit_score:.0f}")
    if fatal:
        reasons.append("Fatal gaps: " + "; ".join(fatal[:3]))
    if confidence < policy.minimum_confidence:
        reasons.append(f"Confidence {confidence:.2f} is below the minimum of {policy.minimum_confidence:.2f}")
    if reasons:
        return Decision(S.ARCHIVED, reasons)
    note = (f"Fit score {score:.0f} meets your minimum of {policy.minimum_fit_score:.0f}, no fatal gaps, "
            f"confidence {confidence:.2f}")
    if result.qualification_recommendation == "ARCHIVE":
        note += " (the model recommended archiving; the policy decides)"
    return Decision(S.QUALIFIED, [note])


# ───────────── claiming ─────────────

@dataclass(frozen=True)
class TaskClaim:
    task_id: int
    job_id: int
    user_id: int
    token: str
    worker_id: str
    attempt: int
    force: bool
    lease_expires_at: datetime


def _require_postgres(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Ranking task claiming requires PostgreSQL (FOR UPDATE SKIP LOCKED)")


def _lock_job(db: Session, job_id: int, *, skip_locked: bool = True) -> Job | None:
    return db.scalars(select(Job).where(Job.id == job_id).with_for_update(skip_locked=skip_locked)
                      .execution_options(populate_existing=True)).first()


def _clear_lease(task: JobRankingTask) -> None:
    task.locked_at = task.locked_by = task.lock_token = task.lock_expires_at = None


def claim(db: Session, *, worker_id: str, limit: int, ready_accounts: Iterable[int], auto_rank_accounts: Iterable[int],
          now: datetime | None = None, lease_seconds: int | None = None) -> list[TaskClaim]:
    """Lease up to `limit` due PENDING tasks and move their jobs to RANKING. Commits.

    Two workers never receive the same task: rows are locked with FOR UPDATE SKIP LOCKED inside the claim, and the
    lease written to the task keeps other workers away afterwards. User-requested (forced) tasks go first and run
    even when auto-ranking is off.
    """
    _require_postgres(db)
    if limit < 1:
        raise ValueError("limit must be at least 1")
    ready = sorted(set(ready_accounts))
    if not ready:
        return []
    auto = sorted(set(auto_rank_accounts) & set(ready)) or [-1]
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().ranking_lease_seconds)
    tasks = db.scalars(
        select(JobRankingTask)
        .where(JobRankingTask.status == PENDING,
               or_(JobRankingTask.next_attempt_at.is_(None), JobRankingTask.next_attempt_at <= now),
               JobRankingTask.user_id.in_(ready),
               or_(JobRankingTask.force.is_(True), JobRankingTask.user_id.in_(auto)))
        .order_by(JobRankingTask.force.desc(), JobRankingTask.next_attempt_at.asc().nulls_first(), JobRankingTask.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=JobRankingTask)
        .execution_options(populate_existing=True)
    ).all()
    claims = []
    for task in tasks:
        job = _lock_job(db, task.job_id)
        if job is None:
            continue  # the job row is busy (e.g. an import is merging into it); try again next round
        if job.state is not S.RANK_PENDING:
            task.status, task.completed_at = SKIPPED, now
            task.last_error = f"The job is {job.state.value}, no longer waiting for ranking"
            continue
        transition(db, job, S.RANKING, actor=worker_id, reason=f"Ranking attempt {task.attempt_count + 1}", now=now)
        task.status, task.locked_at, task.locked_by = PROCESSING, now, worker_id[:200]
        task.lock_token, task.lock_expires_at = uuid.uuid4().hex, now + lease
        task.attempt_count += 1
        task.last_attempt_at, task.started_at, task.next_attempt_at = now, now, None
        claims.append(TaskClaim(task.id, task.job_id, task.user_id, task.lock_token, task.locked_by, task.attempt_count,
                                task.force, task.lock_expires_at))
    db.commit()
    return claims


def _owned(db: Session, claimed: TaskClaim) -> tuple[JobRankingTask, Job | None]:
    """Re-lock the task and its job, and prove this worker still holds the task."""
    task = db.scalars(select(JobRankingTask).where(JobRankingTask.id == claimed.task_id).with_for_update()
                      .execution_options(populate_existing=True)).one_or_none()
    if task is None or task.status != PROCESSING or task.lock_token != claimed.token:
        db.rollback()
        raise LeaseLost(f"Ranking task {claimed.task_id} is no longer held by {claimed.worker_id}")
    return task, _lock_job(db, task.job_id, skip_locked=False)


# ───────────── retries and recovery ─────────────

def classify(exc: Exception) -> tuple[ErrorCategory, bool, bool]:
    """(category, retryable, wait): `wait` means the account must change something; no attempt is used."""
    if isinstance(exc, LLMConfigError):
        return ErrorCategory.CONFIGURATION, False, True
    if isinstance(exc, (LLMTimeoutError, TimeoutError)):
        return ErrorCategory.TIMEOUT, True, False
    if isinstance(exc, LLMRateLimitError):
        return ErrorCategory.RATE_LIMITED, True, False
    if isinstance(exc, (LLMResponseError, RankingSchemaError)):
        return ErrorCategory.VALIDATION, True, False  # the model may answer correctly next time
    if isinstance(exc, LLMError):
        return ErrorCategory.EXTERNAL_SERVICE, True, False
    return ErrorCategory.UNKNOWN, True, False


def retry_delay(attempt: int) -> timedelta:
    settings = get_settings()
    seconds = settings.ranking_retry_base_seconds * 2 ** max(0, attempt - 1)
    return timedelta(seconds=min(settings.ranking_retry_max_seconds, seconds))


def _retry_or_fail(db: Session, task: JobRankingTask, job: Job | None, category: ErrorCategory, message: str, *,
                   retryable: bool, actor: str, now: datetime) -> str:
    """Record a failed attempt: retry with backoff while attempts remain, otherwise FAILED. Never archives."""
    message = message[:MAX_ERROR_LENGTH]
    _clear_lease(task)
    task.last_error, task.error_category = message, category
    if job is not None:
        job.last_error, job.error_category = message, category
    if retryable and task.attempt_count < get_settings().ranking_max_attempts:
        task.status, task.next_attempt_at = PENDING, now + retry_delay(task.attempt_count)
        if job is not None and job.state is S.RANKING:
            transition(db, job, S.RANK_PENDING, actor=actor, now=now,
                       reason=f"Attempt {task.attempt_count} failed ({category.value}); will retry: {message[:300]}")
        return "retrying"
    task.status, task.completed_at, task.next_attempt_at = FAILED, now, None
    if job is not None:
        if job.state is S.RANKING:
            transition(db, job, S.RANK_PENDING, actor=actor, now=now, reason=f"Attempt {task.attempt_count} failed")
        if job.state is S.RANK_PENDING:
            job.retryable = False
            transition(db, job, S.FAILED, actor=actor, now=now,
                       reason=f"Ranking gave up after {task.attempt_count} attempt(s): {category.value}: {message[:300]}")
    return "failed"


def recover_expired_leases(db: Session, *, now: datetime | None = None, limit: int = 100) -> int:
    """Crash recovery: tasks whose worker died are retried, or failed when out of attempts. Commits."""
    _require_postgres(db)
    now = now or utcnow()
    tasks = db.scalars(select(JobRankingTask).where(JobRankingTask.status == PROCESSING,
                                                    JobRankingTask.lock_expires_at <= now)
                       .order_by(JobRankingTask.lock_expires_at).limit(limit).with_for_update(skip_locked=True)
                       .execution_options(populate_existing=True)).all()
    recovered = 0
    for task in tasks:
        job = _lock_job(db, task.job_id)
        message = (f"Ranking worker {task.locked_by} stopped before finishing attempt {task.attempt_count}: "
                   f"its lease expired at {task.lock_expires_at.isoformat()}")
        _retry_or_fail(db, task, job, ErrorCategory.LEASE_EXPIRED, message, retryable=True, actor="lease-recovery",
                       now=now)
        recovered += 1
    db.commit()
    return recovered


# ───────────── processing one task ─────────────

@dataclass(frozen=True)
class RankingInputs:
    profile_id: int
    resume_id: int
    resume_version: int
    resume_content_hash: str
    profile_hash: str
    evidence_hash: str
    job_hash: str
    verified_ids: frozenset[int]
    policy: RankingPolicy


def _wait(db: Session, claimed: TaskClaim, message: str, now: datetime) -> str:
    """The account cannot be ranked yet: hand the task back without using an attempt."""
    task, job = _owned(db, claimed)
    _clear_lease(task)
    task.status, task.attempt_count = PENDING, max(0, task.attempt_count - 1)
    task.next_attempt_at = now + timedelta(seconds=get_settings().ranking_wait_seconds)
    task.last_error, task.error_category = message[:MAX_ERROR_LENGTH], ErrorCategory.CONFIGURATION
    if job is not None:
        job.last_error, job.error_category = message[:MAX_ERROR_LENGTH], ErrorCategory.CONFIGURATION
        if job.state is S.RANKING:
            transition(db, job, S.RANK_PENDING, actor=claimed.worker_id, reason=f"Waiting: {message[:300]}", now=now)
    db.commit()
    return "waiting"


def _skip(db: Session, claimed: TaskClaim, message: str, now: datetime) -> str:
    task, _job = _owned(db, claimed)
    _clear_lease(task)
    task.status, task.completed_at, task.last_error = SKIPPED, now, message
    db.commit()
    return "superseded"


def _failed(db: Session, claimed: TaskClaim, exc: Exception, now: datetime) -> str:
    category, retryable, wait = classify(exc)
    message = f"{type(exc).__name__}: {exc}"
    if wait:
        return _wait(db, claimed, message, now)
    task, job = _owned(db, claimed)
    outcome = _retry_or_fail(db, task, job, category, message, retryable=retryable, actor=claimed.worker_id, now=now)
    db.commit()
    return outcome


def _identical(db: Session, job_id: int, inputs: RankingInputs) -> JobRanking | None:
    return db.scalars(select(JobRanking).where(
        JobRanking.job_id == job_id, JobRanking.forced.is_(False),
        JobRanking.candidate_profile_id == inputs.profile_id,
        JobRanking.resume_content_hash == inputs.resume_content_hash,
        JobRanking.prompt_version == RANKING_PROMPT_VERSION, JobRanking.profile_hash == inputs.profile_hash,
        JobRanking.evidence_hash == inputs.evidence_hash, JobRanking.job_hash == inputs.job_hash)
        .order_by(desc(JobRanking.version))).first()


def legacy_fit_analysis(ranking: JobRanking, decision: Decision) -> dict[str, Any]:
    """The ranking in the shape the dashboard and the tailoring prompt read from jobs.fit_analysis."""
    return {
        "score": ranking.overall_fit_score, "summary": ranking.reasoning_summary,
        "strong_matches": ranking.strong_matches, "gaps": ranking.missing_requirements,
        "must_have_missing": ranking.fatal_gaps, "keywords": [],
        "application_strategy": "; ".join(ranking.recommended_resume_focus), "confidence": ranking.confidence,
        "overall_fit_score": ranking.overall_fit_score, "candidate_fit_score": ranking.candidate_fit_score,
        "resume_fit_score": ranking.resume_fit_score, "seniority_match_score": ranking.seniority_match_score,
        "skills_match_score": ranking.skills_match_score, "leadership_match_score": ranking.leadership_match_score,
        "domain_match_score": ranking.domain_match_score, "location_match_score": ranking.location_match_score,
        "partial_matches": ranking.partial_matches, "missing_requirements": ranking.missing_requirements,
        "fatal_gaps": ranking.fatal_gaps, "resume_gaps": ranking.resume_gaps,
        "relevant_evidence_ids": ranking.relevant_evidence_ids,
        "recommended_resume_focus": ranking.recommended_resume_focus,
        "qualification_recommendation": ranking.qualification_recommendation,
        "decision": decision.state.value, "decision_reasons": decision.reasons,
        "ranking_id": ranking.id, "ranking_version": ranking.version, "prompt_version": ranking.prompt_version,
    }


def _complete(db: Session, claimed: TaskClaim, inputs: RankingInputs, now: datetime, *,
              existing: JobRanking | None = None, output: RankingOutput | None = None,
              discarded: list[int] | None = None, llm: LLMResult | None = None) -> str:
    task, job = _owned(db, claimed)
    if job is None or job.state is not S.RANKING:
        _clear_lease(task)
        task.status, task.completed_at = SKIPPED, now
        task.last_error = "The job left RANKING before its result was saved"
        db.commit()
        return "superseded"
    ranking = existing
    if ranking is None:
        decision = qualify(output, inputs.policy)
        version = (db.scalar(select(func.max(JobRanking.version)).where(JobRanking.job_id == job.id)) or 0) + 1
        ranking = JobRanking(
            user_id=job.user_id, job_id=job.id, version=version, forced=claimed.force, trigger=task.reason,
            candidate_profile_id=inputs.profile_id, resume_id=inputs.resume_id, resume_version=inputs.resume_version,
            resume_content_hash=inputs.resume_content_hash, profile_hash=inputs.profile_hash,
            evidence_hash=inputs.evidence_hash, job_hash=inputs.job_hash,
            **output.model_dump(exclude={"job_id"}), discarded_evidence_ids=discarded or [],
            decision=decision.state.value, decision_reasons=decision.reasons, policy=inputs.policy.public(),
            provider=llm.provider[:30], model=llm.model[:300], prompt_version=RANKING_PROMPT_VERSION,
            raw_output=llm.data, created_at=now)
        db.add(ranking)
        try:
            db.flush()
        except IntegrityError:  # an identical automatic ranking was stored meanwhile: use that one
            db.rollback()
            existing = _identical(db, claimed.job_id, inputs)
            if existing is None:
                raise
            return _complete(db, claimed, inputs, now, existing=existing)
    decision = qualify(ranking, inputs.policy)  # always the current policy, also for a reused ranking
    transition(db, job, S.RANKED, actor=claimed.worker_id, now=now,
               reason=(f"Ranking v{ranking.version}: fit {ranking.overall_fit_score:.0f}, candidate "
                       f"{ranking.candidate_fit_score:.0f}, resume {ranking.resume_fit_score:.0f}, "
                       f"confidence {ranking.confidence:.2f}" + (" (reused identical inputs)" if existing else "")))
    transition(db, job, decision.state, actor="ranking-policy", reason="; ".join(decision.reasons), now=now)
    job.fit_score, job.fit_analysis = ranking.overall_fit_score, legacy_fit_analysis(ranking, decision)
    if job.triaged_at is None and job.status in ("discovered", "shortlisted", "archived"):
        job.status = "shortlisted" if decision.state is S.QUALIFIED else "archived"  # dashboard mirror only
    job.last_error = job.error_category = None
    _clear_lease(task)
    task.status, task.completed_at, task.next_attempt_at = COMPLETED, now, None
    task.ranking_id, task.candidate_profile_id, task.resume_id = ranking.id, inputs.profile_id, inputs.resume_id
    task.last_error = task.error_category = None
    db.commit()
    return "qualified" if decision.state is S.QUALIFIED else "archived"


def _process(db: Session, claimed: TaskClaim, clock: Callable[[], datetime]) -> str:
    job = db.get(Job, claimed.job_id)
    if job is None:
        return _skip(db, claimed, "The job was deleted", clock())
    profile = _profile(db, claimed.user_id)
    problem = "No candidate profile yet: fill in your profile in Settings" if profile is None \
        else resume_problem(profile.master_resume)
    if problem:
        return _wait(db, claimed, problem, clock())
    snapshot = resume_snapshot(db, profile)
    job, profile = db.get(Job, claimed.job_id), _profile(db, claimed.user_id)  # reloaded after the snapshot commit
    evidence = relevant_evidence(db, claimed.user_id, job, get_settings().ranking_max_evidence)
    inputs = RankingInputs(
        profile_id=profile.id, resume_id=snapshot.id, resume_version=snapshot.version,
        resume_content_hash=snapshot.content_hash, profile_hash=_hash(ranking_profile_facts(profile)),
        evidence_hash=_hash(evidence), job_hash=_hash({**posting_facts(job), "description": job.description or ""}),
        verified_ids=frozenset(item["id"] for item in evidence), policy=policy_for(db, claimed.user_id))
    if not claimed.force:
        existing = _identical(db, job.id, inputs)
        if existing is not None:  # the same inputs were already ranked: no second model call
            return _complete(db, claimed, inputs, clock(), existing=existing)
    prompt = build_ranking_prompt(candidate_profile=profile, resume=snapshot.content, evidence=evidence, job=job)
    try:
        result = LLMClient.for_user(db, claimed.user_id).generate_prompt(prompt)
        output, discarded = validate_output(result.data, job.id, set(inputs.verified_ids))
    except Exception as exc:  # classified below; a provider failure never archives a job
        db.rollback()
        return _failed(db, claimed, exc, clock())
    return _complete(db, claimed, inputs, clock(), output=output, discarded=discarded, llm=result)


def process(sessions: SessionFactory, claimed: TaskClaim, clock: Callable[[], datetime] = utcnow) -> str:
    """Rank one claimed task in a session bound to its owner, saving the result on its own. Returns the outcome."""
    with sessions() as db:
        db.info["user_id"] = claimed.user_id
        try:
            return _process(db, claimed, clock)
        except LeaseLost:
            db.rollback()
            return "lost"
        except Exception as exc:  # e.g. the database failed mid-save: record it on the task like any failure
            db.rollback()
            try:
                return _failed(db, claimed, exc, clock())
            except LeaseLost:
                return "lost"


# ───────────── the scheduler's sweep ─────────────

def _commit(db: Session) -> bool:
    try:
        db.commit()
        return True
    except IntegrityError:  # a user request queued the same job at the same moment
        db.rollback()
        return False


@dataclass(frozen=True)
class CurrentInputs:
    profile_id: int
    profile_hash: str
    resume_hash: str


def _current_inputs(db: Session, user_id: int) -> CurrentInputs | None:
    profile = _profile(db, user_id)
    if profile is None or resume_problem(profile.master_resume):
        return None
    return CurrentInputs(profile.id, _hash(ranking_profile_facts(profile)), content_hash(profile.master_resume or ""))


def _queue_stale(db: Session, now: datetime, limit: int) -> int:
    """Qualified jobs whose latest ranking used an older profile, resume or prompt go back to RANK_PENDING."""
    latest = (select(JobRanking.job_id, func.max(JobRanking.version).label("version"))
              .group_by(JobRanking.job_id).subquery())
    rows = db.execute(
        select(Job.id, Job.user_id, JobRanking.candidate_profile_id, JobRanking.resume_content_hash,
               JobRanking.profile_hash, JobRanking.prompt_version)
        .join(latest, latest.c.job_id == Job.id)
        .join(JobRanking, and_(JobRanking.job_id == Job.id, JobRanking.version == latest.c.version))
        .where(Job.state == S.QUALIFIED).order_by(Job.id)).all()
    current: dict[int, CurrentInputs | None] = {}
    queued = 0
    for job_id, user_id, profile_id, resume_hash, profile_hash, prompt_version in rows:
        if queued >= limit:
            break
        if user_id not in current:
            current[user_id] = _current_inputs(db, user_id)
        now_inputs = current[user_id]
        if now_inputs is None:
            continue
        reason = ("prompt_changed" if prompt_version != RANKING_PROMPT_VERSION
                  else "resume_changed" if resume_hash != now_inputs.resume_hash
                  else "profile_changed" if profile_hash != now_inputs.profile_hash or profile_id != now_inputs.profile_id
                  else None)
        if reason is None:
            continue
        job = _lock_job(db, job_id)
        if job is None or job.state is not S.QUALIFIED:
            db.rollback()
            continue
        transition(db, job, S.RANK_PENDING, actor="ranker", reason=RERANK_REASONS[reason], now=now)
        enqueue(db, job, reason=reason, now=now)
        queued += _commit(db)
    return queued


def _recheck_salary(db: Session, now: datetime, limit: int, policy_of: Callable[[int], RankingPolicy]) -> int:
    """Ranking never waits for salary enrichment; once a salary is known, a qualified job that breaks policy is archived."""
    archived = 0
    owners = db.scalars(select(Job.user_id).where(Job.state == S.QUALIFIED).distinct()).all()
    for user_id in owners:
        policy = policy_of(user_id)
        if policy.minimum_salary is None:
            continue
        candidates = [job.id for job in db.scalars(select(Job).where(Job.user_id == user_id, Job.state == S.QUALIFIED)
                                                   .order_by(Job.id).limit(limit)).all()
                      if salary_filter_reason(job, policy)]
        db.rollback()  # read without locks; only jobs that break the policy are locked below
        for job_id in candidates:
            job = _lock_job(db, job_id)
            reason = salary_filter_reason(job, policy) if job is not None and job.state is S.QUALIFIED else None
            if not reason:
                db.rollback()
                continue
            transition(db, job, S.ARCHIVED, actor="ranking-policy", reason=reason, now=now)
            if job.triaged_at is None and job.status in ("discovered", "shortlisted"):
                job.status = "archived"
            archived += _commit(db)
    return archived


def sweep(db: Session, *, now: datetime | None = None, limit: int | None = None) -> dict[str, int]:
    """Deterministic bookkeeping before claiming. Bounded, idempotent, and never calls a model. Commits per job."""
    settings = get_settings()
    now = now or utcnow()
    limit = limit or settings.ranking_enqueue_batch_size
    counts: Counter = Counter()
    counts["recovered"] = recover_expired_leases(db, now=now)
    policies: dict[int, RankingPolicy] = {}

    def policy_of(user_id: int) -> RankingPolicy:
        if user_id not in policies:
            policies[user_id] = policy_for(db, user_id)
        return policies[user_id]

    # Jobs created by any path (manual, from a URL, Lever, Greenhouse, older imports) are filtered and queued here.
    for job_id in db.scalars(select(Job.id).where(Job.state.in_(PRE_RANK_STATES), Job.user_id.is_not(None))
                             .order_by(Job.id).limit(limit)).all():
        job = _lock_job(db, job_id)
        if job is None or job.state not in PRE_RANK_STATES:
            db.rollback()
            continue
        skipped = queue_job(db, job, policy=policy_of(job.user_id), actor="ranker", now=now)
        if _commit(db):
            counts["filtered_out" if skipped else "queued"] += 1
    # Jobs waiting without a task.
    orphaned = ~exists().where(JobRankingTask.job_id == Job.id,
                               JobRankingTask.status.in_(ACTIVE_RANKING_TASK_STATUSES))
    for job_id in db.scalars(select(Job.id).where(Job.state == S.RANK_PENDING, orphaned).order_by(Job.id)
                             .limit(limit)).all():
        job = _lock_job(db, job_id)
        if job is None or job.state is not S.RANK_PENDING:
            db.rollback()
            continue
        enqueue(db, job, now=now)
        counts["queued"] += _commit(db)
    counts["archived_by_salary_policy"] = _recheck_salary(db, now, limit, policy_of)
    if settings.ranking_auto_rerank:
        counts["rerank_queued"] = _queue_stale(db, now, limit)
    return {key: counts.get(key, 0) for key in ("recovered", "queued", "filtered_out", "archived_by_salary_policy",
                                                "rerank_queued")}


# ───────────── read models for the API ─────────────

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def task_dict(task: JobRankingTask) -> dict[str, Any]:
    return {"id": task.id, "job_id": task.job_id, "status": task.status, "reason": task.reason, "force": task.force,
            "attempt_count": task.attempt_count, "last_error": task.last_error,
            "error_category": task.error_category.value if task.error_category else None,
            "next_attempt_at": _iso(task.next_attempt_at), "created_at": _iso(task.created_at),
            "started_at": _iso(task.started_at), "completed_at": _iso(task.completed_at),
            "candidate_profile_id": task.candidate_profile_id, "resume_id": task.resume_id,
            "ranking_id": task.ranking_id}


def task_summaries(db: Session, job_ids: Iterable[int]) -> list[dict[str, Any]]:
    """The latest ranking task of each job."""
    ids = list(dict.fromkeys(job_ids))
    if not ids:
        return []
    latest = select(func.max(JobRankingTask.id)).where(JobRankingTask.job_id.in_(ids)).group_by(JobRankingTask.job_id)
    return [task_dict(t) for t in db.scalars(select(JobRankingTask).where(JobRankingTask.id.in_(latest))
                                             .order_by(JobRankingTask.job_id)).all()]


def ranking_dict(ranking: JobRanking) -> dict[str, Any]:
    fields = ("id", "job_id", "version", "forced", "trigger", "candidate_profile_id", "resume_id", "resume_version",
              "resume_content_hash", "overall_fit_score", "confidence", "candidate_fit_score", "resume_fit_score",
              "seniority_match_score", "skills_match_score", "leadership_match_score", "domain_match_score",
              "location_match_score", "strong_matches", "partial_matches", "missing_requirements", "fatal_gaps",
              "resume_gaps", "relevant_evidence_ids", "discarded_evidence_ids", "recommended_resume_focus",
              "qualification_recommendation", "reasoning_summary", "decision", "decision_reasons", "policy",
              "provider", "model", "prompt_version")
    return {**{name: getattr(ranking, name) for name in fields}, "created_at": _iso(ranking.created_at)}


TRACKER_WINDOW = timedelta(hours=24)
TRACKER_LIMIT = 200


def tracker(db: Session, user_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Where the account's ranking work stands: waiting, running, retrying and finished in the last day.

    Only this account's tasks and jobs are read. `retried` marks a failed task that has a newer task for its job.
    """
    now = now or utcnow()
    since = now - TRACKER_WINDOW
    count = lambda *where: db.scalar(select(func.count()).select_from(JobRankingTask).where(  # noqa: E731
        JobRankingTask.user_id == user_id, *where)) or 0
    active_where = JobRankingTask.status.in_(ACTIVE_RANKING_TASK_STATUSES)
    finished_where = and_(JobRankingTask.status.in_((COMPLETED, FAILED, SKIPPED)), JobRankingTask.completed_at >= since)
    not_due = and_(JobRankingTask.next_attempt_at.is_not(None), JobRankingTask.next_attempt_at > now)
    counts = {
        "filtering": db.scalar(select(func.count(Job.id)).where(Job.user_id == user_id,
                                                                Job.state.in_(PRE_RANK_STATES))) or 0,
        "queued": count(JobRankingTask.status == PENDING, or_(JobRankingTask.next_attempt_at.is_(None),
                                                              JobRankingTask.next_attempt_at <= now)),
        "retry_scheduled": count(JobRankingTask.status == PENDING, not_due, JobRankingTask.attempt_count > 0),
        "waiting": count(JobRankingTask.status == PENDING, not_due, JobRankingTask.attempt_count == 0),
        "processing": count(JobRankingTask.status == PROCESSING),
        "completed": count(JobRankingTask.status == COMPLETED, JobRankingTask.completed_at >= since),
        "failed": count(JobRankingTask.status == FAILED, JobRankingTask.completed_at >= since),
        "skipped": count(JobRankingTask.status == SKIPPED, JobRankingTask.completed_at >= since),
    }
    rows = db.execute(
        select(JobRankingTask, Job.title, Job.company, Job.state, JobRanking)
        .join(Job, Job.id == JobRankingTask.job_id)
        .outerjoin(JobRanking, JobRanking.id == JobRankingTask.ranking_id)
        .where(JobRankingTask.user_id == user_id, or_(active_where, finished_where))
        .order_by(JobRankingTask.status.in_(ACTIVE_RANKING_TASK_STATUSES).desc(),
                  func.coalesce(JobRankingTask.completed_at, JobRankingTask.created_at).desc(), JobRankingTask.id.desc())
        .limit(TRACKER_LIMIT * 2)).all()
    latest_task = dict(db.execute(select(JobRankingTask.job_id, func.max(JobRankingTask.id))
                                  .where(JobRankingTask.user_id == user_id,
                                         JobRankingTask.job_id.in_({row[0].job_id for row in rows}))
                                  .group_by(JobRankingTask.job_id)).all()) if rows else {}

    def row_dict(task: JobRankingTask, title: str, company: str, state: S, ranking: JobRanking | None) -> dict[str, Any]:
        return {**task_dict(task), "title": title, "company": company, "job_state": state.value,
                "max_attempts": get_settings().ranking_max_attempts,
                "lease_expires_at": _iso(task.lock_expires_at),
                "retried": task.status == FAILED and latest_task.get(task.job_id, task.id) > task.id,
                "ranking": None if ranking is None else {
                    "version": ranking.version, "overall_fit_score": ranking.overall_fit_score,
                    "candidate_fit_score": ranking.candidate_fit_score, "resume_fit_score": ranking.resume_fit_score,
                    "confidence": ranking.confidence, "decision": ranking.decision,
                    "decision_reasons": ranking.decision_reasons, "model": ranking.model}}

    def active_order(row: Any) -> tuple:
        task = row[0]
        due_later = task.next_attempt_at is not None and task.next_attempt_at > now
        # Ranking now, then due in claim order (user requests first), then retries and waits by when they are due.
        return (task.status != PROCESSING, due_later, not task.force, task.next_attempt_at or task.created_at, task.id)

    active = [row_dict(*row) for row in sorted((row for row in rows if row[0].status in ACTIVE_RANKING_TASK_STATUSES),
                                               key=active_order)][:TRACKER_LIMIT]
    recent = [row_dict(*row) for row in rows if row[0].status not in ACTIVE_RANKING_TASK_STATUSES][:TRACKER_LIMIT]
    decisions = Counter(r["ranking"]["decision"] for r in recent if r["ranking"])
    in_flight = counts["queued"] + counts["retry_scheduled"] + counts["waiting"] + counts["processing"]
    finished = counts["completed"] + counts["failed"] + counts["skipped"]
    return {
        "generated_at": now.isoformat(),
        "blocked": readiness(db, user_id),
        "auto_rank": auto_rank_on(db, user_id),
        "counts": {**counts, "qualified": decisions.get("QUALIFIED", 0), "archived": decisions.get("ARCHIVED", 0)},
        "progress": {"finished": finished, "total": finished + in_flight},
        "active": active,
        "recent": recent,
    }
