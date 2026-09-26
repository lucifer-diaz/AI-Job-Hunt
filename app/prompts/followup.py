"""Follow-up drafts for submitted applications (stage: followup). Drafts are never sent automatically.

Output contract: app.prompts.contracts.FollowUpResult.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    candidate_identity,
    context_value,
    instructions,
    job_facts,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import FollowUpResult, describe_fields

# v3: typed output contract; the candidate is a name and headline; the job is its title, company and location
# (a follow-up does not need the whole posting).
FOLLOWUP_PROMPT_VERSION = "v3"
FOLLOWUP_MARKER = "FOLLOW_UP"

FOLLOWUP_SYSTEM = instructions(
    "Write a short, professional, low-pressure follow-up for a job application that has been submitted.",
    UNTRUSTED_DATA_RULE,
    "Mention the candidate's background only with facts from VERIFIED RESUME FACTS and VERIFIED EVIDENCE, and list "
    "the evidence ids you use. Use the company, the role, the application date and the primary recruiter's name "
    "when known. The tone can be natural and conversational, but never state a fact that is not provided, and "
    "never imply an interview, relationship or reply that is not shown. No pressure, guilt or urgency. If PREVIOUS "
    "FOLLOW-UPS are listed, do not repeat their wording and keep this one lighter.",
    JSON_ONLY_RULE,
    describe_fields(FollowUpResult),
)


def build_followup_prompt(candidate_profile: Any, job: Any = None, application: dict[str, Any] | None = None,
                          recruiters: list[dict[str, str]] | None = None, previous_followups: list[str] | None = None,
                          context: Any = None, resume: str = "", evidence: list[dict[str, Any]] | None = None) -> Prompt:
    """With a follow-up context (app.services.context_builder.build_followup_context) only a few verified highlights
    relevant to the role are sent; `resume` and `evidence` are for callers without one."""
    if context is not None:
        resume_section = untrusted("VERIFIED RESUME FACTS", "untrusted_resume_facts",
                                   context_value(context, "resume_text", ""), limit=6000)
        evidence = context_value(context, "relevant_evidence", [])
    else:
        resume_section = untrusted("RESUME", "untrusted_resume", resume, limit=6000)
    user = user_message(
        FOLLOWUP_MARKER,
        section("CANDIDATE", to_json(candidate_identity(candidate_profile))),
        resume_section,
        section("VERIFIED EVIDENCE", to_json(evidence or [], limit=8000)),
        section("JOB", to_json(job_facts(job))),
        section("APPLICATION", to_json(application or {})),
        section("RECRUITERS (the first is the primary contact)", to_json(recruiters)) if recruiters else "",
        untrusted("PREVIOUS FOLLOW-UPS", "untrusted_previous_followups", "\n---\n".join(previous_followups))
        if previous_followups else "",
    )
    return Prompt("followup", FOLLOWUP_PROMPT_VERSION, FOLLOWUP_MARKER, FOLLOWUP_SYSTEM, user, FollowUpResult)
