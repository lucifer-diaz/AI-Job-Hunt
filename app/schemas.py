from __future__ import annotations

from pydantic import BaseModel, Field


class ProfileIn(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    headline: str | None = None
    target_roles: list[str] | None = None
    preferred_locations: list[str] | None = None
    work_authorization: str | None = None
    compensation_notes: str | None = None
    master_resume: str | None = None
    search_preferences: dict | None = None


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


class RecruiterIn(BaseModel):
    recruiter_name: str = ""
    recruiter_email: str = ""
    notes: str = ""
