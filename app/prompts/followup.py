"""Follow-up drafts for submitted applications (stage: followup). Drafts are never sent automatically."""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    context_value,
    instructions,
    job_facts,
    profile_facts,
    section,
    to_json,
    untrusted,
    user_message,
)

FOLLOWUP_PROMPT_VERSION = "v2"
FOLLOWUP_MARKER = "FOLLOW_UP"

FOLLOWUP_SYSTEM = instructions(
    "Write a short, professional, low-pressure follow-up for a job application that has been submitted.",
    UNTRUSTED_DATA_RULE,
    "Use only facts from VERIFIED RESUME FACTS and VERIFIED EVIDENCE when mentioning the candidate's background. "
    "Use the company, the role, the application date and the primary recruiter's name when known. Do not "
    "claim facts that are not provided, and never imply an interview, relationship or reply that is not "
    "shown. Keep it brief and polite: no pressure, guilt or urgency. If PREVIOUS FOLLOW-UPS are listed, do "
    "not repeat their wording and keep this one lighter.",
    JSON_ONLY_RULE,
    "Fields: draft (string).",
)


def build_followup_prompt(candidate_profile: Any, resume: str = "", evidence: list[dict[str, Any]] | None = None,
                          job: Any = None, application: dict[str, Any] | None = None,
                          recruiters: list[dict[str, str]] | None = None, previous_followups: list[str] | None = None,
                          context: Any = None) -> Prompt:
    """With a follow-up context only a few verified highlights relevant to the role are sent."""
    if context is not None:
        resume_section = untrusted("VERIFIED RESUME FACTS", "untrusted_resume_facts",
                                   context_value(context, "resume_text", ""), limit=6000)
        evidence = context_value(context, "relevant_evidence", [])
    else:
        resume_section = untrusted("RESUME", "untrusted_resume", resume, limit=22000)
    user = user_message(
        FOLLOWUP_MARKER,
        section("CANDIDATE PROFILE", to_json(profile_facts(candidate_profile, include_name=True))),
        resume_section,
        section("VERIFIED EVIDENCE", to_json(evidence or [], limit=32000)),
        section("JOB", to_json(job_facts(job))),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", getattr(job, "description", ""), limit=24000),
        section("APPLICATION", to_json(application or {})),
        section("RECRUITERS (the first is the primary contact)", to_json(recruiters or [])),
        untrusted("PREVIOUS FOLLOW-UPS", "untrusted_previous_followups", "\n---\n".join(previous_followups))
        if previous_followups else "",
    )
    return Prompt("followup", FOLLOWUP_PROMPT_VERSION, FOLLOWUP_MARKER, FOLLOWUP_SYSTEM, user)
