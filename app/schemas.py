from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


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
