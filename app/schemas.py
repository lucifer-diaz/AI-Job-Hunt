from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    mode: Literal["tailor", "skip"] = "tailor"  # skip = send the master resume unchanged
    regenerate: bool = False  # also redo jobs that already have a packet


class TailorTasksCancelIn(BaseModel):
    ids: list[int] | None = None  # None cancels every queued task


class RecruiterIn(BaseModel):
    recruiter_name: str = ""
    recruiter_email: str = ""
    notes: str = ""


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


class ProfilePatch(ProfileIn):
    """Only supplied fields are written; nested preferences are merged."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_null_resume(self) -> ProfilePatch:
        if "master_resume" in self.model_fields_set and self.master_resume is None:
            raise ValueError("Use an empty string to clear resume text")
        return self


class SettingsPatch(SettingsIn):
    """Omitted values remain saved; blank AI overrides restore inheritance."""

    model_config = ConfigDict(extra="forbid")


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
