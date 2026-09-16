"""Follow-up drafts for submitted applications (stage: followup). Drafts are never sent automatically."""
from __future__ import annotations

from typing import Any

from app.prompts.common import (JSON_ONLY_RULE, UNTRUSTED_DATA_RULE, Prompt, instructions, job_facts, profile_facts,
                                section, to_json, untrusted, user_message)

FOLLOWUP_PROMPT_VERSION = "v1"
FOLLOWUP_MARKER = "FOLLOW_UP"

FOLLOWUP_SYSTEM = instructions(
    "Write a short, professional, low-pressure follow-up for a job application that has been submitted.",
    UNTRUSTED_DATA_RULE,
    "Use the company, the role, the application date and the primary recruiter's name when known. Do not "
    "claim facts that are not provided, and never imply an interview, relationship or reply that is not "
    "shown. Keep it brief and polite: no pressure, guilt or urgency. If PREVIOUS FOLLOW-UPS are listed, do "
    "not repeat their wording and keep this one lighter.",
    JSON_ONLY_RULE,
    "Fields: draft (string).",
)


def build_followup_prompt(candidate_profile: Any, resume: str, evidence: list[dict[str, Any]], job: Any,
                          application: dict[str, Any], recruiters: list[dict[str, str]],
                          previous_followups: list[str] | None = None) -> Prompt:
    user = user_message(
        FOLLOWUP_MARKER,
        section("CANDIDATE PROFILE", to_json(profile_facts(candidate_profile, include_name=True))),
        untrusted("RESUME", "untrusted_resume", resume, limit=22000),
        section("VERIFIED EVIDENCE", to_json(evidence, limit=32000)),
        section("JOB", to_json(job_facts(job))),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", getattr(job, "description", ""), limit=24000),
        section("APPLICATION", to_json(application)),
        section("RECRUITERS (the first is the primary contact)", to_json(recruiters)),
        untrusted("PREVIOUS FOLLOW-UPS", "untrusted_previous_followups", "\n---\n".join(previous_followups))
        if previous_followups else "",
    )
    return Prompt("followup", FOLLOWUP_PROMPT_VERSION, FOLLOWUP_MARKER, FOLLOWUP_SYSTEM, user)
