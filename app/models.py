from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, Numeric,
                        String, Text,
                        UniqueConstraint, text)
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


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


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
    structured_resumes: Mapped[list[Resume]] = relationship(back_populates="user", passive_deletes=True)
    resume_data_changes: Mapped[list[ResumeDataChange]] = relationship(back_populates="user", passive_deletes=True)
    knowledge_answers: Mapped[list[KnowledgeAnswer]] = relationship(back_populates="user", passive_deletes=True)
    evidence: Mapped[list[Evidence]] = relationship(back_populates="user", passive_deletes=True)
    jobs: Mapped[list[Job]] = relationship(back_populates="user", passive_deletes=True)
    search_runs: Mapped[list[SearchRun]] = relationship(back_populates="user", passive_deletes=True)
    search_profiles: Mapped[list[SearchProfile]] = relationship(back_populates="user", passive_deletes=True)
    applications: Mapped[list[Application]] = relationship(back_populates="user", passive_deletes=True)
    resume_variants: Mapped[list[ResumeVariant]] = relationship(back_populates="user", passive_deletes=True)
    tailor_tasks: Mapped[list[TailorTask]] = relationship(back_populates="user", passive_deletes=True)
    followups: Mapped[list[FollowUp]] = relationship(back_populates="user", passive_deletes=True)
    application_events: Mapped[list[ApplicationEvent]] = relationship(back_populates="user", passive_deletes=True)
    transitions: Mapped[list[WorkflowTransition]] = relationship(back_populates="user", passive_deletes=True)
    resume_versions: Mapped[list[ResumeVersion]] = relationship(back_populates="user", passive_deletes=True)
    job_requirements: Mapped[list[JobRequirementEvidence]] = relationship(back_populates="user", passive_deletes=True)
    job_parse_tasks: Mapped[list[JobDescriptionParseTask]] = relationship(back_populates="user", passive_deletes=True)
    job_structured_changes: Mapped[list[JobStructuredDataChange]] = relationship(back_populates="user",
                                                                                 passive_deletes=True)
    ranking_tasks: Mapped[list[JobRankingTask]] = relationship(back_populates="user", passive_deletes=True)
    rankings: Mapped[list[JobRanking]] = relationship(back_populates="user", passive_deletes=True)
    optimization_tasks: Mapped[list[AIOptimizationTask]] = relationship(back_populates="user", passive_deletes=True)
    optimization_reports: Mapped[list[AIOptimizationReport]] = relationship(back_populates="user", passive_deletes=True)


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
    gemini_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
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


RESUME_VERIFICATION_STATUSES = ("UNVERIFIED", "PARTIALLY_VERIFIED", "VERIFIED", "NEEDS_REVIEW")
RESUME_CHANGE_TYPES = ("ADD", "UPDATE", "REMOVE", "REORDER", "VERIFY", "REJECT", "UNVERIFY", "PARSE",
                       "REPARSE_ACCEPT", "REPARSE_KEEP", "VERIFY_RESUME")
RESUME_CHANGE_SOURCES = ("PARSER", "USER", "SYSTEM")


class Resume(Base):
    """One version of the candidate's resume: the original file, its cleaned raw text and the structured data.

    Versions are never overwritten: a new upload (or new master text) is a new row. `raw_text` is the read-only
    reference; `structured_data` (app.services.structured_resume) is what AI workflows read, and only the parts
    the user verified are trusted (app.services.resume_verification).
    """

    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "version", name="uq_resumes_profile_version"),
        CheckConstraint(f"verification_status IN ({', '.join(repr(v) for v in RESUME_VERIFICATION_STATUSES)})",
                        name="ck_resumes_verification_status"),
        Index("uq_resumes_active_profile", "candidate_profile_id", unique=True,
              postgresql_where=text("is_active IS TRUE"), sqlite_where=text("is_active = 1")),
        Index("uq_resumes_default_profile", "candidate_profile_id", unique=True,
              postgresql_where=text("is_default IS TRUE"), sqlite_where=text("is_default = 1")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resumes")
    user: Mapped[User] = relationship(back_populates="structured_resumes")
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE", name="fk_resumes_candidate_profile_id"), index=True)
    # The uploaded file, while its row exists. The source_* columns keep the provenance after it is deleted.
    resume_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_documents.id", ondelete="SET NULL", name="fk_resumes_resume_document_id"), nullable=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload | manual_text | migration
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_media_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    source_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    raw_text_hash: Mapped[str] = mapped_column(String(64))
    raw_text: Mapped[str] = mapped_column(Text, default="")
    structured_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parser: Mapped[str | None] = mapped_column(String(20), nullable=True)  # deterministic | llm
    parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Incremented on every change to structured_data; edits must name the revision they started from.
    revision: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_status: Mapped[str] = mapped_column(String(20), default="UNVERIFIED")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A re-parse waiting for the user to keep or replace each differing value.
    pending_reparse: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ResumeDataChange(Base):
    """Append-only history of how each structured resume field changed, and who changed it."""

    __tablename__ = "resume_data_changes"
    __table_args__ = (
        CheckConstraint(f"change_type IN ({', '.join(repr(v) for v in RESUME_CHANGE_TYPES)})",
                        name="ck_resume_data_changes_change_type"),
        CheckConstraint(f"source IN ({', '.join(repr(v) for v in RESUME_CHANGE_SOURCES)})",
                        name="ck_resume_data_changes_source"),
        Index("ix_resume_data_changes_resume_changed", "resume_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_data_changes")
    user: Mapped[User] = relationship(back_populates="resume_data_changes")
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE", name="fk_resume_data_changes_resume_id"))
    field_path: Mapped[str] = mapped_column(String(300))
    item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    old_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    change_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(10), default="USER")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=now)


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
    # Provenance. `resume` evidence is created only from structured resume items the user verified
    # (app.services.resume_verification); `fact_key` lets one fact be reused across resume versions.
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")  # manual | resume | llm_extraction
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL", name="fk_evidence_resume_id"), nullable=True, index=True)
    structured_item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fact_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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


JD_STATUSES = ("RAW", "NORMALIZED", "PARSED", "NEEDS_REVIEW", "MANUALLY_CORRECTED")
JD_PARSE_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")
ACTIVE_JD_PARSE_STATUSES = ("PENDING", "PROCESSING")
REQUIREMENT_TYPES = ("SKILL", "EXPERIENCE", "RESPONSIBILITY", "LEADERSHIP", "EDUCATION", "LOCATION",
                     "WORK_AUTHORIZATION", "VISA", "COMPENSATION", "DOMAIN", "SENIORITY", "OTHER")
REQUIREMENT_LEVELS = ("REQUIRED", "PREFERRED", "INFORMATIONAL", "UNKNOWN")
JOB_CHANGE_SOURCES = ("PARSER", "USER", "SYSTEM")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "source_ref", name="uq_jobs_user_source_ref"),
        CheckConstraint(_in_check("state", WorkflowState), name="ck_jobs_state"),
        CheckConstraint(_in_check("previous_state", WorkflowState), name="ck_jobs_previous_state"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_jobs_error_category"),
        CheckConstraint(f"structured_description_status IN ({_values(JD_STATUSES)})",
                        name="ck_jobs_structured_description_status"),
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
    # Structured job description (app.services.job_description). `description` above stays the employer's raw text
    # and is never rewritten; these hold the cleaned text and the machine-readable version built from it.
    normalized_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_description: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    structured_description_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    structured_description_status: Mapped[str] = mapped_column(String(25), default="RAW", server_default="RAW")
    structured_description_parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The raw description the structured version was parsed from, and a re-parse waiting for the user's choices.
    structured_description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    structured_description_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

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
    tailor_tasks: Mapped[list[TailorTask]] = relationship(back_populates="job", cascade="all, delete-orphan")
    ranking_tasks: Mapped[list[JobRankingTask]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                               passive_deletes=True)
    rankings: Mapped[list[JobRanking]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                      passive_deletes=True, order_by="JobRanking.version")
    requirements: Mapped[list[JobRequirementEvidence]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                      passive_deletes=True)
    parse_tasks: Mapped[list[JobDescriptionParseTask]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                                      passive_deletes=True)
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


class JobRequirementEvidence(Base):
    """One requirement the posting states (or one signal the system derived), with the sentence it came from.

    `is_explicit` rows quote the employer; `is_derived` rows are the system's inference and are never presented as
    something the employer said.
    """

    __tablename__ = "job_requirement_evidence"
    __table_args__ = (
        CheckConstraint(f"requirement_type IN ({_values(REQUIREMENT_TYPES)})",
                        name="ck_job_requirement_evidence_type"),
        CheckConstraint(f"requirement_level IN ({_values(REQUIREMENT_LEVELS)})",
                        name="ck_job_requirement_evidence_level"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_job_requirement_evidence_confidence"),
        CheckConstraint("origin IN ('PARSER', 'USER')", name="ck_job_requirement_evidence_origin"),
        Index("ix_job_requirement_evidence_job_type", "job_id", "requirement_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_requirement_evidence")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_requirement_evidence_job_id"), index=True)
    item_id: Mapped[str] = mapped_column(String(60))  # stable within one parse, e.g. "req_001"
    requirement_type: Mapped[str] = mapped_column(String(25))
    normalized_value: Mapped[str] = mapped_column(String(300))
    requirement_level: Mapped[str] = mapped_column(String(15), default="UNKNOWN")
    source_text: Mapped[str] = mapped_column(Text, default="")
    source_section: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_explicit: Mapped[bool] = mapped_column(Boolean, default=True)
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Who settled this row's level: the parser, or the user (a correction or a re-parse decision). A parser never
    # replaces a USER row.
    origin: Mapped[str] = mapped_column(String(10), default="PARSER", server_default="PARSER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    job: Mapped[Job] = relationship(back_populates="requirements")
    user: Mapped[User] = relationship(back_populates="job_requirements")


class JobStructuredDataChange(Base):
    """Append-only history of how a job's structured description changed, and who changed it."""

    __tablename__ = "job_structured_data_changes"
    __table_args__ = (
        CheckConstraint(f"change_source IN ({_values(JOB_CHANGE_SOURCES)})",
                        name="ck_job_structured_data_changes_source"),
        Index("ix_job_structured_data_changes_job_created", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_structured_data_changes")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_structured_data_changes_job_id"))
    field_path: Mapped[str] = mapped_column(String(300))
    old_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    change_source: Mapped[str] = mapped_column(String(10), default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship()
    user: Mapped[User] = relationship(back_populates="job_structured_changes")


class JobDescriptionParseTask(Base):
    """One queued parse of a job description, claimed by the scheduler like a ranking task.

    A job has at most one PENDING or PROCESSING task at a time (uq_jd_parse_tasks_active_job), and a task carries
    the hash of the description it was created for, so the same text is never queued twice.
    """

    __tablename__ = "job_description_parse_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(JD_PARSE_STATUSES)})", name="ck_jd_parse_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_jd_parse_tasks_attempt_count"),
        Index("uq_jd_parse_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_JD_PARSE_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_JD_PARSE_STATUSES)})")),
        Index("ix_jd_parse_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_description_parse_tasks")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE", name="fk_jd_parse_tasks_job_id"), index=True)
    description_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    reason: Mapped[str] = mapped_column(String(40), default="new")  # new | user_request | description_changed
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="parse_tasks")
    user: Mapped[User] = relationship(back_populates="job_parse_tasks")


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


class TailorTask(Base):
    """One queued, running or finished background tailoring run for a job (app.services.tailor_tasks)."""

    __tablename__ = "tailor_tasks"
    __table_args__ = (
        CheckConstraint("mode IN ('tailor', 'skip')", name="ck_tailor_tasks_mode"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_tailor_tasks_status"),
        Index("ix_tailor_tasks_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = _owner("tailor_tasks")
    mode: Mapped[str] = mapped_column(String(10), default="tailor")
    regenerate: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    stage: Mapped[str] = mapped_column(String(20), default="queued")  # queued | ranking | tailoring | reviewing | done
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="tailor_tasks")
    user: Mapped[User] = relationship(back_populates="tailor_tasks")


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
    # Every recruiter for this application: [{name, email, phone}]. The first is mirrored above (app.services.contacts).
    contacts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
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
    # Interviews: the time zone they were scheduled in and an optional contact (timezone, contact_name,
    # contact_email, contact_phone).
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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


class ResumeVersion(Base):
    """An immutable snapshot of the resume text a candidate profile used, one row per distinct content.

    Rankings point at the snapshot they were made with, so they stay reproducible after the resume is edited,
    replaced or its uploaded file is deleted.
    """

    __tablename__ = "resume_versions"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "content_hash", name="uq_resume_versions_profile_hash"),
        UniqueConstraint("candidate_profile_id", "version", name="uq_resume_versions_profile_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("resume_versions")
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE", name="fk_resume_versions_candidate_profile_id"),
        index=True)
    # The uploaded file the text came from, when it matches one. The snapshot outlives the file.
    resume_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_documents.id", ondelete="SET NULL", name="fk_resume_versions_resume_document_id"),
        nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="resume_versions")


RANKING_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE_RANKING_TASK_STATUSES = ("PENDING", "PROCESSING")
RANKING_DECISIONS = ("QUALIFIED", "ARCHIVED")


class JobRanking(Base):
    """One versioned, append-only fit analysis of a job, with the exact inputs it was made from.

    Automatic rankings are unique per job, candidate profile, resume content, prompt version, profile facts,
    evidence and posting (uq_job_rankings_inputs), so repeating the same work never creates a second row.
    A ranking the user explicitly asked for (forced) is always a new version.
    """

    __tablename__ = "job_rankings"
    __table_args__ = (
        UniqueConstraint("job_id", "version", name="uq_job_rankings_job_version"),
        Index("uq_job_rankings_inputs", "job_id", "candidate_profile_id", "resume_content_hash", "prompt_version",
              "profile_hash", "evidence_hash", "job_hash", unique=True,
              postgresql_where=text("forced IS FALSE"), sqlite_where=text("forced = 0")),
        CheckConstraint(f"decision IN ({_values(RANKING_DECISIONS)})", name="ck_job_rankings_decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_rankings")
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_rankings_job_id"),
                                        index=True)
    version: Mapped[int] = mapped_column(Integer)
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger: Mapped[str] = mapped_column(String(40), default="new")
    # Inputs: which profile and which exact resume content, and hashes of everything else the model saw.
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE", name="fk_job_rankings_candidate_profile_id"),
        index=True)
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="CASCADE", name="fk_job_rankings_resume_id"), index=True)
    resume_version: Mapped[int] = mapped_column(Integer)
    resume_content_hash: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    job_hash: Mapped[str] = mapped_column(String(64))
    # Structured output, validated before it is stored.
    overall_fit_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    candidate_fit_score: Mapped[float] = mapped_column(Float)
    resume_fit_score: Mapped[float] = mapped_column(Float)
    seniority_match_score: Mapped[float] = mapped_column(Float)
    skills_match_score: Mapped[float] = mapped_column(Float)
    leadership_match_score: Mapped[float] = mapped_column(Float)
    domain_match_score: Mapped[float] = mapped_column(Float)
    location_match_score: Mapped[float] = mapped_column(Float)
    strong_matches: Mapped[list[str]] = mapped_column(JSON, default=list)
    partial_matches: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_requirements: Mapped[list[str]] = mapped_column(JSON, default=list)
    fatal_gaps: Mapped[list[str]] = mapped_column(JSON, default=list)
    resume_gaps: Mapped[list[str]] = mapped_column(JSON, default=list)
    relevant_evidence_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    # Evidence ids the model cited that were not in the verified evidence it was given; never trusted.
    discarded_evidence_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    recommended_resume_focus: Mapped[list[str]] = mapped_column(JSON, default=list)
    qualification_recommendation: Mapped[str] = mapped_column(String(20))
    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    # The deterministic decision made from this output and the policy in force when it was stored.
    decision: Mapped[str] = mapped_column(String(20))
    decision_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(300))
    prompt_version: Mapped[str] = mapped_column(String(20))
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    job: Mapped[Job] = relationship(back_populates="rankings")
    user: Mapped[User] = relationship(back_populates="rankings")


class JobRankingTask(Base):
    """A unit of ranking work, claimed by the ranking worker with FOR UPDATE SKIP LOCKED and a lease.

    A job has at most one PENDING or PROCESSING task at a time (uq_job_ranking_tasks_active_job).
    """

    __tablename__ = "job_ranking_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(RANKING_TASK_STATUSES)})", name="ck_job_ranking_tasks_status"),
        CheckConstraint("attempt_count >= 0", name="ck_job_ranking_tasks_attempt_count"),
        CheckConstraint(_in_check("error_category", ErrorCategory), name="ck_job_ranking_tasks_error_category"),
        Index("uq_job_ranking_tasks_active_job", "job_id", unique=True,
              postgresql_where=text(f"status IN ({_values(ACTIVE_RANKING_TASK_STATUSES)})"),
              sqlite_where=text(f"status IN ({_values(ACTIVE_RANKING_TASK_STATUSES)})")),
        Index("ix_job_ranking_tasks_claim", "status", "next_attempt_at"),
        Index("ix_job_ranking_tasks_lock_expires_at", "lock_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("job_ranking_tasks")
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE", name="fk_job_ranking_tasks_job_id"),
                                        index=True)
    # The profile and resume snapshot the ranking used; filled in when the task runs.
    candidate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="SET NULL", name="fk_job_ranking_tasks_candidate_profile_id"),
        nullable=True)
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL", name="fk_job_ranking_tasks_resume_id"), nullable=True)
    ranking_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_rankings.id", ondelete="SET NULL", name="fk_job_ranking_tasks_ranking_id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    # Why the ranking was requested: new, user_request, profile_changed, resume_changed, prompt_changed.
    reason: Mapped[str] = mapped_column(String(40), default="new")
    # A user-requested ranking: runs even with auto-ranking off and always stores a new version.
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[ErrorCategory | None] = mapped_column(_enum_type(ErrorCategory), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="ranking_tasks")
    user: Mapped[User] = relationship(back_populates="ranking_tasks")


LLM_STAGES = ("RANKER", "TAILOR", "REVIEWER", "RESUME_PARSER", "APPLICATION", "FOLLOW_UP", "INBOX", "COMPANY_INSIGHTS",
              "ADVISOR", "OTHER")
LLM_ERROR_TYPES = ("TIMEOUT", "RATE_LIMIT", "INVALID_JSON", "SCHEMA_VALIDATION", "PROVIDER_ERROR", "CONTEXT_OVERFLOW",
                   "CONFIGURATION", "OTHER")
OPTIMIZATION_TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")


class LLMRequestLog(Base):
    """One LLM request made anywhere in the system, recorded by the shared LLM layer (app.services.llm_telemetry).

    Metadata and token counts only: prompt and response text are stored only when LLM_PROMPT_DEBUG is on.
    Metrics a provider does not report stay NULL unless they can be estimated, and `usage_source` says which.
    Not tenancy-filtered automatically (a request may belong to no account); app.services.monitoring always
    filters by user_id.
    """

    __tablename__ = "llm_request_logs"
    __table_args__ = (
        CheckConstraint(f"stage IN ({_values(LLM_STAGES)})", name="ck_llm_request_logs_stage"),
        CheckConstraint("status IN ('SUCCEEDED', 'FAILED')", name="ck_llm_request_logs_status"),
        CheckConstraint(f"error_type IN ({_values(LLM_ERROR_TYPES)})", name="ck_llm_request_logs_error_type"),
        Index("ix_llm_request_logs_user_created", "user_id", "created_at"),
        Index("ix_llm_request_logs_stage_created", "stage", "created_at"),
        Index("ix_llm_request_logs_model_created", "model", "created_at"),
        Index("ix_llm_request_logs_created_at", "created_at"),
        UniqueConstraint("request_id", name="uq_llm_request_logs_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_llm_request_logs_user_id"), nullable=True)
    workflow_type: Mapped[str | None] = mapped_column(String(30), nullable=True)  # job | application | profile | system | agent
    workflow_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String(30), default="OTHER")
    provider: Mapped[str] = mapped_column(String(30))
    requested_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(20), default="none")  # provider_reported | estimated | none
    estimated_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(20), default="unknown")  # free | pricing_table | provider_reported | unknown
    pricing_effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SUCCEEDED")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    structured_output_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The posting side of context selection (app.services.job_context): the raw description against what was sent.
    raw_job_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_job_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_context_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Dynamic context selection (app.services.context_builder): what could have been sent versus what was.
    full_available_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    debug_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LLMUsageDaily(Base):
    """Daily totals kept after raw request rows pass LLM_TELEMETRY_RETENTION_DAYS."""

    __tablename__ = "llm_usage_daily"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "stage", "provider", "model", "prompt_version", name="uq_llm_usage_daily_key"),
        Index("ix_llm_usage_daily_user_day", "user_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_llm_usage_daily_user_id"), nullable=True)
    day: Mapped[date] = mapped_column(Date)
    stage: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(300), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    structured_output_failures: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    unpriced_requests: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_max: Mapped[int] = mapped_column(Integer, default=0)
    context_tokens_sum: Mapped[int] = mapped_column(Integer, default=0)


class AIOptimizationReport(Base):
    """Recommendations from the AI Optimization Advisor. Advice only: nothing in it is ever applied automatically."""

    __tablename__ = "ai_optimization_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_optimization_reports")
    time_range_start: Mapped[datetime] = mapped_column(DateTime)
    time_range_end: Mapped[datetime] = mapped_column(DateTime)
    summary: Mapped[str] = mapped_column(Text, default="")
    cost_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    latency_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    context_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    model_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    prompt_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reliability_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    recommendations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    estimated_savings: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The exact deterministic aggregates the report was built from.
    aggregates: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_by: Mapped[str] = mapped_column(String(10), default="rules")  # llm | rules
    model_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship(back_populates="optimization_reports")


class AIOptimizationTask(Base):
    """A queued analysis of an account's LLM telemetry, processed by the scheduler's ai_optimization job."""

    __tablename__ = "ai_optimization_tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(OPTIMIZATION_TASK_STATUSES)})", name="ck_ai_optimization_tasks_status"),
        Index("uq_ai_optimization_tasks_active_user", "user_id", unique=True,
              postgresql_where=text("status IN ('PENDING', 'PROCESSING')"),
              sqlite_where=text("status IN ('PENDING', 'PROCESSING')")),
        Index("ix_ai_optimization_tasks_claim", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = _owner("ai_optimization_tasks")
    time_range_start: Mapped[datetime] = mapped_column(DateTime)
    time_range_end: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | scheduled
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_optimization_reports.id", ondelete="SET NULL", name="fk_ai_optimization_tasks_report_id"),
        nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="optimization_tasks")


# Registers the per-user isolation hooks on every session; needs the models above.
from app import tenancy  # noqa: E402,F401
