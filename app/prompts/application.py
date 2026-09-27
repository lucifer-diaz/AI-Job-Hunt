"""Application questions: interpret an unusual form question and answer it only from known candidate data.

Not called by the app yet: Claude Desktop answers form questions over MCP from app.services.knowledge, and the
backend saves answers by hand in the Knowledge bank. The prompt is kept ready and tested so the worker can be
switched on without re-deriving its rules. Output contract: app.prompts.contracts.ApplicationAnswerResult.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    context_value,
    instructions,
    resume_facts,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import APPLICATION_DECISIONS, ApplicationAnswerResult, describe_fields

# v3: verified resume facts are application data, not untrusted text; each fact is sent once.
# v2: answers from a prepared context (verified facts and verified knowledge answers, not the raw resume), a
# NEEDS_INPUT decision for questions only the candidate can answer, and a typed output contract.
APPLICATION_QUESTION_PROMPT_VERSION = "v3"
APPLICATION_QUESTION_MARKER = "APPLICATION_QUESTION"
__all__ = ["APPLICATION_DECISIONS", "APPLICATION_QUESTION_PROMPT_VERSION", "build_application_question_prompt"]

APPLICATION_QUESTION_SYSTEM = instructions(
    "You interpret one question from a job application form and answer it for the candidate, or say why you cannot.",
    UNTRUSTED_DATA_RULE + " The question and its options come from the employer's form.",
    NO_INVENTION_RULE + " Answer only from CANDIDATE PROFILE, KNOWN ANSWERS, VERIFIED RESUME FACTS and VERIFIED "
    "EVIDENCE, and cite evidence ids for factual answers. Never guess work authorization, visa or sponsorship, salary, "
    "years of experience, identity, demographic, legal or contact answers.",
    """decision:
  ANSWER            the supplied data answers the question directly,
  NEEDS_INPUT       only the candidate can answer it (a preference, a sensitive or legal answer, a missing fact),
  UNKNOWN           an optional question the data does not answer,
  SKIP_APPLICATION  the truthful answer conflicts with CANDIDATE POLICY.""",
    JSON_ONLY_RULE,
    describe_fields(ApplicationAnswerResult),
)


def build_application_question_prompt(question: str, context: Any, options: list[str] | None = None) -> Prompt:
    """Build from an application answer context (app.services.context_builder.build_application_answer_context)."""
    knowledge = context_value(context, "knowledge", [])
    user = user_message(
        APPLICATION_QUESTION_MARKER,
        untrusted("QUESTION", "untrusted_question", question, limit=2000),
        untrusted("ANSWER OPTIONS", "untrusted_options", to_json(options)) if options else "",
        section("CANDIDATE PROFILE", to_json(context_value(context, "candidate", {}) or {})),
        section("CANDIDATE POLICY", to_json(context_value(context, "policy", {}) or {})),
        section("KNOWN ANSWERS", to_json(knowledge)) if knowledge else "",
        section("VERIFIED RESUME FACTS", to_json(resume_facts(context), limit=16000)),
        section("VERIFIED EVIDENCE", to_json(context_value(context, "relevant_evidence", []), limit=16000)),
    )
    return Prompt("application_question", APPLICATION_QUESTION_PROMPT_VERSION, APPLICATION_QUESTION_MARKER,
                  APPLICATION_QUESTION_SYSTEM, user, ApplicationAnswerResult)
