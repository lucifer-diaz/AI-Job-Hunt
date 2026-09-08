from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    master_resume: str = ""
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
    sites: list[str] = Field(
        default_factory=lambda: ["linkedin", "indeed", "glassdoor"], min_length=1
    )
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


class RecruiterIn(BaseModel):
    recruiter_name: str = ""
    recruiter_email: str = ""
    notes: str = ""


ProviderName = Literal["openrouter", "openai", "anthropic", "mock"]


class ApplicationSettingsIn(BaseModel):
    active_provider: ProviderName = "openrouter"
    openrouter_model: str = Field(default="openrouter/free", min_length=1, max_length=300)
    openai_model: str = Field(default="gpt-5.6-luna", min_length=1, max_length=300)
    anthropic_model: str = Field(default="claude-sonnet-5", min_length=1, max_length=300)
    approval_mode: Literal["manual", "review_required"] = "manual"
    min_fit_score: int = Field(default=72, ge=0, le=100)
    followup_days: int = Field(default=5, ge=1, le=90)
    max_jobs_per_batch: int = Field(default=30, ge=1, le=200)

    @field_validator("openrouter_model", "openai_model", "anthropic_model")
    @classmethod
    def model_name_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("model names must be non-empty and cannot contain whitespace")
        return value


class CredentialState(BaseModel):
    configured: bool
    masked_suffix: str | None = None


class ApplicationSettingsOut(ApplicationSettingsIn):
    credentials: dict[ProviderName, CredentialState]


class ConnectionTestOut(BaseModel):
    ok: bool
    provider: ProviderName
    message: str
