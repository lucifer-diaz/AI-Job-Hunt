from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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


AnswerType = Literal["text", "textarea", "boolean", "single_choice", "multi_choice", "number", "date"]
VerificationState = Literal["unverified", "verified", "rejected"]


class ATSQuestionIn(BaseModel):
    text: str = Field(min_length=1)
    category: str = "general"
    answer_type: AnswerType = "text"
    allowed_choices: list[Any] = Field(default_factory=list)
    sensitivity: str = "none"


class ATSQuestionUpdate(BaseModel):
    text: str | None = None
    category: str | None = None
    answer_type: AnswerType | None = None
    allowed_choices: list[Any] | None = None
    sensitivity: str | None = None
    archived: bool | None = None


class ATSAnswerIn(BaseModel):
    answer: Any
    verification_state: VerificationState = "unverified"
    source_application_id: int | None = None
    expires_at: datetime | None = None


class ATSObservedQuestion(ATSQuestionIn):
    external_key: str = Field(min_length=1, max_length=300)


class ATSInspectionIn(BaseModel):
    questions: list[ATSObservedQuestion] = Field(min_length=1)


class ATSResolutionIn(BaseModel):
    answer: Any
    verification_state: VerificationState = "verified"
    expires_at: datetime | None = None
