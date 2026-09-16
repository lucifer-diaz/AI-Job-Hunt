"""Application questions: interpret an unusual form question and answer it only from known candidate data.

Not called by the app yet: answers are currently saved by hand in the Knowledge bank.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (JSON_ONLY_RULE, NO_INVENTION_RULE, UNTRUSTED_DATA_RULE, Prompt, instructions,
                                profile_facts, section, to_json, untrusted, user_message)

APPLICATION_QUESTION_PROMPT_VERSION = "v1"
APPLICATION_QUESTION_MARKER = "APPLICATION_QUESTION"
APPLICATION_DECISIONS = ("ANSWER", "UNKNOWN", "SKIP_APPLICATION")

APPLICATION_QUESTION_SYSTEM = instructions(
    "You interpret a question from a job application form and answer it for the candidate.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Answer only from CANDIDATE PROFILE, CANDIDATE POLICY, RESUME and VERIFIED EVIDENCE. "
    "Never guess. Never invent work authorization, visa or sponsorship status, salary or compensation, years of "
    "experience, identity, demographic or contact data.",
    f"""Decide:
  ANSWER when the supplied data answers the question directly,
  UNKNOWN when it does not,
  SKIP_APPLICATION when the question is required, cannot be answered from the data, or the true answer
    conflicts with CANDIDATE POLICY.""",
    JSON_ONLY_RULE,
    f"""Fields:
  decision (one of {", ".join(APPLICATION_DECISIONS)}),
  answer (string or null; null unless decision is ANSWER),
  sources (array of strings: which of profile, policy, resume, evidence the answer came from),
  evidence_ids (array of ints),
  reason (string).""",
)


def build_application_question_prompt(question: str, candidate_profile: Any, candidate_policy: dict[str, Any] | None,
                                      resume: str, evidence: list[dict[str, Any]],
                                      options: list[str] | None = None) -> Prompt:
    user = user_message(
        APPLICATION_QUESTION_MARKER,
        untrusted("QUESTION", "untrusted_question", question, limit=2000),
        untrusted("ANSWER OPTIONS", "untrusted_options", to_json(options)) if options else "",
        section("CANDIDATE PROFILE", to_json(profile_facts(candidate_profile, include_name=True))),
        section("CANDIDATE POLICY", to_json(candidate_policy or {})),
        untrusted("RESUME", "untrusted_resume", resume, limit=22000),
        section("VERIFIED EVIDENCE", to_json(evidence, limit=32000)),
    )
    return Prompt("application_question", APPLICATION_QUESTION_PROMPT_VERSION, APPLICATION_QUESTION_MARKER,
                  APPLICATION_QUESTION_SYSTEM, user)
