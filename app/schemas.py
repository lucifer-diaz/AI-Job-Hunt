from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProfileIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    headline: str = ""
    target_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    work_authorization: str = ""
    compensation_notes: str = ""
    # Left out means "leave the saved text alone", so a stale form cannot overwrite a newer resume.
    master_resume: str | None = None
    search_preferences: dict = Field(default_factory=dict)


class EvidenceIn(BaseModel):
    company: str = ""
    role: str = ""
    claim: str
    context: str = ""
    skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    verified: bool = True
    # The resumes this evidence belongs to (Evidence Bank tags); none = it applies to every resume.
    resume_ids: list[int] | None = None


class ManualJobIn(BaseModel):
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str


class ApifySearchIn(BaseModel):
    search_terms: list[str] = Field(min_length=1, max_length=5)
    location: str = ""
    locations: list[str] = Field(default_factory=list, max_length=5)
    sites: list[str] = Field(default_factory=lambda: ["linkedin", "indeed", "glassdoor"])
    max_results: int = Field(default=20, ge=1, le=100)
    hours_old: int | None = Field(default=168, ge=1, le=8760)
    is_remote: bool = False
    job_type: str = ""
    easy_apply: bool = False
    country_indeed: str = ""
    linkedin_fetch_description: bool = True
    actor_id: str = ""
    custom_input: dict = Field(default_factory=dict)
    raw_actor_input: dict = Field(default_factory=dict)


class ApiKeysIn(BaseModel):
    """The user's own provider keys. Omitted (null) keys are unchanged; an empty string removes a key."""
    openrouter: str | None = Field(default=None, max_length=500)
    openai: str | None = Field(default=None, max_length=500)
    anthropic: str | None = Field(default=None, max_length=500)
    gemini: str | None = Field(default=None, max_length=500)
    apify: str | None = Field(default=None, max_length=500)


class BulkTailorIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    mode: Literal["tailor", "skip"] = "tailor"  # skip = send the master resume unchanged
    regenerate: bool = False  # also redo jobs that already have a packet
    batch_size: int = Field(default=1, ge=1, le=5)  # tailoring is slow; small batches keep requests short


class TailorTasksIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    # skip = send the master resume unchanged; review = only review the latest packet (its review never ran or failed)
    mode: Literal["tailor", "skip", "review"] = "tailor"
    regenerate: bool = False  # also redo jobs that already have a packet
    # The page target for these jobs (1, 2 or 3; app.services.resume_length). None = the default in Settings.
    pages: Literal[1, 2, 3] | None = None


class TailorTasksCancelIn(BaseModel):
    ids: list[int] | None = None  # None cancels every queued task


class RecruiterIn(BaseModel):
    recruiter_name: str = ""
    recruiter_email: str = ""
    notes: str = ""
    # The token "Start submission" (POST /api/jobs/{id}/submission/start) returned; omitted when a person reports an
    # application they made without starting it here.
    claim_token: str | None = None
    confirmation_id: str | None = None


class TailoringLengthIn(BaseModel):
    """The default length of tailored resumes (app.services.resume_length). Omitted values keep what is saved."""

    model_config = ConfigDict(extra="forbid")

    pages: Literal[1, 2, 3] | None = None
    # Bullets per role, most recent role first, for a page target ("1", "2", "3"); [] restores the preset.
    budgets: dict[Literal["1", "2", "3"], list[Annotated[int, Field(ge=0, le=8)]]] | None = None

    @field_validator("budgets")
    @classmethod
    def _positions(cls, value):
        if value and any(len(budget) > 12 for budget in value.values()):
            raise ValueError("At most 12 roles can have their own bullet budget")
        return value


class SettingsIn(BaseModel):
    # Blank AI values mean "use the .env value".
    llm_provider: str = ""
    model: str = Field(default="", max_length=300)
    stage_models: dict[str, str] = Field(default_factory=dict)
    require_free: bool | None = None
    application_mode: str = "manual_approval"
    min_review_score: int = Field(default=70, ge=0, le=100)
    # backend: the configured model API tailors and reviews. claude_desktop: Claude Desktop does it over MCP.
    # Omitted keeps the saved mode.
    execution_mode: str | None = None
    # How much work may run at once for this account. Bounded by the server's own ceilings, so the upper limit
    # is validated against RANKING_CONCURRENCY / TAILOR_CONCURRENCY rather than a constant here.
    ranking_concurrency: int | None = Field(default=None, ge=1, le=64)
    tailoring_concurrency: int | None = Field(default=None, ge=1, le=64)
    # 0 = rank whenever the server looks; higher spreads this account's ranking over a longer stretch.
    ranking_interval_minutes: int | None = Field(default=None, ge=0, le=1440)
    # How long to leave a provider alone after it answers 429 (app.services.backoff). Bounds mirror that module.
    rate_limit_backoff: bool | None = None
    rate_limit_base_seconds: int | None = Field(default=None, ge=1, le=600)
    rate_limit_multiplier: float | None = Field(default=None, ge=1.0, le=10.0)
    rate_limit_max_seconds: int | None = Field(default=None, ge=5, le=86_400)
    # Queue qualified jobs for tailoring when ranking finishes (app.services.auto_tailor). Omitted keeps what is
    # saved; a minimum fit score of 0 means every qualified job.
    auto_tailor: bool | None = None
    auto_tailor_min_fit_score: int | None = Field(default=None, ge=0, le=100)
    tailoring_length: TailoringLengthIn | None = None


class ProfilePatch(ProfileIn):
    """Only supplied fields are written; nested preferences are merged."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_null_resume(self) -> ProfilePatch:
        if "master_resume" in self.model_fields_set and self.master_resume is None:
            raise ValueError("master_resume cannot be null: send resume text, or leave it out")
        return self


class SettingsPatch(SettingsIn):
    """Omitted values remain saved; blank AI overrides restore inheritance."""

    model_config = ConfigDict(extra="forbid")


class ApproveIn(BaseModel):
    """Approving a packet. `override` waives the reviewer minimum for this packet only, and is recorded."""

    override: bool = False
    note: str = Field(default="", max_length=2000)


class ApplicationUpdateIn(BaseModel):
    notes: str = ""
    recruiter_name: str = ""
    recruiter_email: str = ""
    status: str | None = None


class KnowledgeIn(BaseModel):
    question: str = Field(min_length=2)
    answer: str = ""
    category: str = "general"
    answer_type: str = "text"
    verified: bool = False
    sensitive: bool = False
    source_job_id: int | None = None


class McpTokenIn(BaseModel):
    name: str = Field(default="Claude Desktop", min_length=1, max_length=100)
