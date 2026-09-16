from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base
from app.services.lifecycle import ErrorCategory, WorkflowState, parse_state, validate_transition


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _enum_type(enum: type) -> Enum:
    # Stored as VARCHAR; the named CHECK constraints below (and in migration 0002) reject other strings.
    return Enum(enum, native_enum=False, create_constraint=False, length=32, validate_strings=True,
                values_callable=lambda members: [m.value for m in members])


def _in_check(column: str, enum: type) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


def _owner(table: str, unique: bool = False, required: bool = True):
    """The account that owns a row: every owned table carries it, indexed, and the row dies with the account.

    Migration 0007 makes it NOT NULL once every row has an owner; an install that still has pre-accounts
    data keeps it nullable in the database until the first account claims that data.
    """
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE", name=f"fk_{table}_user_id"), nullable=not required,
                         index=True, unique=unique)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(300))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Everything this account owns. Deleting an account cascades in the database, so these never load
    # rows just to delete them (passive_deletes).
    profile: Mapped[CandidateProfile | None] = relationship(back_populates="user", uselist=False, passive_deletes=True)
    settings: Mapped[AppSetting | None] = relationship(back_populates="user", uselist=False, passive_deletes=True)
    resumes: Mapped[list[ResumeDocument]] = relationship(back_populates="user", passive_deletes=True)
    knowledge_answers: Mapped[list[KnowledgeAnswer]] = relationship(back_populates="user", passive_deletes=True)
    evidence: Mapped[list[Evidence]] = relationship(back_populates="user", passive_deletes=True)
    jobs: Mapped[list[Job]] = relationship(back_populates="user", passive_deletes=True)
    search_runs: Mapped[list[SearchRun]] = relationship(back_populates="user", passive_deletes=True)
    search_profiles: Mapped[list[SearchProfile]] = relationship(back_populates="user", passive_deletes=True)
    applications: Mapped[list[Application]] = relationship(back_populates="user", passive_deletes=True)
    resume_variants: Mapped[list[ResumeVariant]] = relationship(back_populates="user", passive_deletes=True)
    followups: Mapped[list[FollowUp]] = relationship(back_populates="user", passive_deletes=True)
    application_events: Mapped[list[ApplicationEvent]] = relationship(back_populates="user", passive_deletes=True)
    transitions: Mapped[list[WorkflowTransition]] = relationship(back_populates="user", passive_deletes=True)


class UserSession(Base):
    """A signed-in browser. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE", name="fk_user_sessions_user_id"),
                                         index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("candidate_profiles", unique=True)
    user: Mapped[User] = relationship(back_populates="profile")
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    linkedin_url: Mapped[str] = mapped_column(String(500), default="")
    github_url: Mapped[str] = mapped_column(String(500), default="")
    headline: Mapped[str] = mapped_column(String(500), default="")
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    work_authorization: Mapped[str] = mapped_column(Text, default="")
    compensation_notes: Mapped[str] = mapped_column(Text, default="")
    master_resume: Mapped[str] = mapped_column(Text, default="")
    search_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("app_settings", unique=True)
    user: Mapped[User] = relationship(back_populates="settings")
    llm_provider: Mapped[str] = mapped_column(String(30), default="openrouter")
    model: Mapped[str] = mapped_column(String(300), default="openrouter/free")
    application_mode: Mapped[str] = mapped_column(String(30), default="manual_approval")
    min_review_score: Mapped[int] = mapped_column(Integer, default=70)
    # Cadence, tracker stages and other small UI preferences.
    preferences: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict, nullable=True)
    # The user's own API keys, encrypted with SECRET_KEY (app.security). Never returned by the API.
    openrouter_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    openai_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    anthropic_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    apify_api_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ResumeDocument(Base):
    __tablename__ = "resume_documents"
    __table_args__ = (UniqueConstraint("user_id", "checksum", name="uq_resume_documents_user_checksum"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_documents")
    user: Mapped[User] = relationship(back_populates="resumes")
    original_filename: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(150))
    storage_path: Mapped[str] = mapped_column(String(1000))
    checksum: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeAnswer(Base):
    __tablename__ = "knowledge_answers"
    __table_args__ = (UniqueConstraint("user_id", "question_key", name="uq_knowledge_user_question_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("knowledge_answers")
    user: Mapped[User] = relationship(back_populates="knowledge_answers")
    question: Mapped[str] = mapped_column(Text)
    question_key: Mapped[str] = mapped_column(String(64), index=True)
    answer: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="general")
    answer_type: Mapped[str] = mapped_column(String(40), default="text")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("evidence")
    user: Mapped[User] = relationship(back_populates="evidence")
    company: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(200), default="")
    claim: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    verified: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SearchProfile(Base):
    """A saved Hunter search the scheduler re-runs on its own cadence (app.services.hunter)."""

    __tablename__ = "search_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_search_profiles_user_name"),
        Index("ix_search_profiles_next_run_at", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("search_profiles")
    user: Mapped[User] = relationship(back_populates="search_profiles")
    name: Mapped[str] = mapped_column(String(120))
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    sites: Mapped[list[str]] = mapped_column(JSON, default=list)
    hours_old: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_results: Mapped[int] = mapped_column(Integer, default=20)
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    job_type: Mapped[str] = mapped_column(String(30), default="")
    country: Mapped[str] = mapped_column(String(60), default="")
    mode: Mapped[str] = mapped_column(String(20), default="primary")
    require_posted_date: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    run_every_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "source_ref", name="uq_jobs_user_source_ref"),
        CheckConstraint(_in_check("state", WorkflowState), name="ck_jobs_state"),
        CheckConstraint(_in_check("previous_state", WorkflowState), name="ck_jobs_previous_state"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_jobs_error_category"),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count"),
        CheckConstraint("(locked_at IS NULL AND locked_by IS NULL AND lock_token IS NULL AND lock_expires_at IS NULL)"
                        " OR (locked_at IS NOT NULL AND locked_by IS NOT NULL AND lock_token IS NOT NULL"
                        " AND lock_expires_at IS NOT NULL)", name="ck_jobs_lease"),
        Index("ix_jobs_workflow_claim", "state", "next_attempt_at"),
        Index("ix_jobs_lock_expires_at", "lock_expires_at"),
        Index("ix_jobs_compensation_checked_at", "compensation_checked_at"),
        # Hunter deduplication: board job ID, then canonical URL, then company + title + city.
        Index("ix_jobs_user_board_job_id", "user_id", "source_board", "source_job_id"),
        Index("ix_jobs_user_canonical_url", "user_id", "canonical_url"),
        Index("ix_jobs_user_canonical_apply_url", "user_id", "canonical_apply_url"),
        Index("ix_jobs_user_dedupe_key", "user_id", "dedupe_key"),
        Index("ix_jobs_posted_at", "posted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("jobs")
    source: Mapped[str] = mapped_column(String(50), default="manual")
    source_ref: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(300))
    location: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(1200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="discovered")
    search_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Canonical workflow state (app.services.lifecycle). `status` above is the legacy dashboard status.
    state: Mapped[WorkflowState] = mapped_column(_enum_type(WorkflowState), default=WorkflowState.DISCOVERED,
                                                 server_default=WorkflowState.DISCOVERED.value)
    previous_state: Mapped[WorkflowState | None] = mapped_column(_enum_type(WorkflowState), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Salary enrichment (app.services.compensation): posting salary plus the status of other sources.
    compensation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    compensation_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Hunter (app.services.hunter): the posting as the board stated it, and why this job was found.
    # posted_at is the employer's posting date and is never replaced by discovered_at; unknown stays NULL.
    source_board: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    direct_apply_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    canonical_apply_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(700), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posted_date_raw: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    job_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    remote_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(100), nullable=True)
    skills: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    experience: Mapped[str | None] = mapped_column(String(200), nullable=True)
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    salary_interval: Mapped[str | None] = mapped_column(String(20), nullable=True)
    salary_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    matched_search_role: Mapped[str | None] = mapped_column(String(300), nullable=True)
    matched_search_location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    apify_actor_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    apify_run_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    search_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_profiles.id", ondelete="SET NULL", name="fk_jobs_search_profile_id"), nullable=True, index=True)
    # Every search that found this posting: role, location, board, actor and run.
    discoveries: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship(back_populates="jobs")
    resumes: Mapped[list[ResumeVariant]] = relationship(back_populates="job", cascade="all, delete-orphan")
    application: Mapped[Application | None] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan")
    transitions: Mapped[list[WorkflowTransition]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                 passive_deletes=True,
                                                                 order_by="WorkflowTransition.id")

    @validates("state")
    def _guard_state(self, _key: str, value: object) -> WorkflowState:
        """No arbitrary strings, and no state change that the lifecycle does not allow."""
        target = parse_state(value)
        current = getattr(self, "state", None)
        if current is not None and current != target:
            validate_transition(current, target, previous_state=self.previous_state)
        return target

    @validates("previous_state")
    def _guard_previous_state(self, _key: str, value: object) -> WorkflowState | None:
        return None if value is None else parse_state(value)


class WorkflowTransition(Base):
    """Append-only history of every workflow state change."""

    __tablename__ = "workflow_transitions"
    __table_args__ = (
        CheckConstraint(_in_check("from_state", WorkflowState), name="ck_workflow_transitions_from_state"),
        CheckConstraint(_in_check("to_state", WorkflowState), name="ck_workflow_transitions_to_state"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_workflow_transitions_error_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("workflow_transitions")
    from_state: Mapped[WorkflowState | None] = mapped_column(_enum_type(WorkflowState), nullable=True)
    to_state: Mapped[WorkflowState] = mapped_column(_enum_type(WorkflowState))
    actor: Mapped[str] = mapped_column(String(200), default="system")
    reason: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="transitions")
    user: Mapped[User] = relationship(back_populates="transitions")


class SchedulerJob(Base):
    """One row per scheduled job: the lease that stops two app instances running it at once, and its last run."""

    __tablename__ = "scheduler_jobs"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_trigger: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class SearchRun(Base):
    __tablename__ = "search_runs"
    __table_args__ = (UniqueConstraint("user_id", "external_run_id", name="uq_search_runs_user_external_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("search_runs")
    user: Mapped[User] = relationship(back_populates="search_runs")
    provider: Mapped[str] = mapped_column(String(50), default="apify")
    external_run_id: Mapped[str] = mapped_column(String(200), index=True)
    actor_id: Mapped[str] = mapped_column(String(300), default="")
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="READY")
    dataset_id: Mapped[str] = mapped_column(String(200), default="")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_jobs: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Hunter run tracking (app.services.hunter). Runs started elsewhere leave these empty.
    adapter: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hunter_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    search_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_profiles.id", ondelete="SET NULL", name="fk_search_runs_search_profile_id"),
        nullable=True, index=True)
    roles: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sites: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    search_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fallback_for_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class ResumeVariant(Base):
    __tablename__ = "resume_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("resume_variants")
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    markdown: Mapped[str] = mapped_column(Text, default="")
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    reviewer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewer_notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="resumes")
    user: Mapped[User] = relationship(back_populates="resume_variants")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[int] = _owner("applications")
    status: Mapped[str] = mapped_column(String(50), default="pending_approval")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    followup_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recruiter_name: Mapped[str] = mapped_column(String(200), default="")
    recruiter_email: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    approved_resume_id: Mapped[int | None] = mapped_column(ForeignKey("resume_variants.id", ondelete="SET NULL"), nullable=True)
    # Post-submission tracker column: applied, acknowledged, screening, interviewing, ..., closed.
    stage: Mapped[str | None] = mapped_column(String(50), default="applied", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="application")
    user: Mapped[User] = relationship(back_populates="applications")
    followups: Mapped[list[FollowUp]] = relationship(back_populates="application", cascade="all, delete-orphan")
    events: Mapped[list[ApplicationEvent]] = relationship(back_populates="application", cascade="all, delete-orphan",
                                                          order_by="ApplicationEvent.occurred_at")


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("application_events")
    kind: Mapped[str] = mapped_column(String(40), default="note")
    title: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    application: Mapped[Application] = relationship(back_populates="events")
    user: Mapped[User] = relationship(back_populates="application_events")


class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("followups")
    due_at: Mapped[datetime] = mapped_column(DateTime)
    channel: Mapped[str] = mapped_column(String(30), default="email")
    draft: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    application: Mapped[Application] = relationship(back_populates="followups")
    user: Mapped[User] = relationship(back_populates="followups")


# Registers the per-user isolation hooks on every session; needs the models above.
from app import tenancy  # noqa: E402,F401
