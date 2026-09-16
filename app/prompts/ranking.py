"""Job ranking: CandidateProfile + active resume + verified Evidence + job → fit analysis (stage: rank)."""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    instructions,
    job_facts,
    profile_facts,
    section,
    to_json,
    untrusted,
    user_message,
)

RANKING_PROMPT_VERSION = "v1"
RANKING_MARKER = "FIT_ANALYSIS"

RANKING_SYSTEM = instructions(
    "You are a rigorous job-fit evaluator.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Judge fit only from CANDIDATE PROFILE, RESUME and VERIFIED EVIDENCE: do not infer "
    "experience that is not stated, and do not reward keywords the candidate's facts do not support. "
    "Do not tailor or rewrite the resume.",
    "Score fit from 0-100. Distinguish hard missing requirements from learnable gaps.",
    JSON_ONLY_RULE,
    """Fields:
  score (number 0-100): the overall fit,
  summary (string),
  strong_matches (array of strings),
  gaps (array of strings): learnable gaps,
  must_have_missing (array of strings): hard requirements the candidate does not meet,
  keywords (array of strings): the posting's important ATS keywords,
  application_strategy (string),
  confidence (number 0-1),
  overall_fit_score (number 0-100): the same value as score,
  candidate_fit_score (number 0-100): fit of the candidate's actual experience,
  resume_fit_score (number 0-100): how well the resume as written shows that fit,
  partial_matches (array of strings),
  missing_requirements (array of strings),
  fatal_gaps (array of strings): requirements that rule the candidate out,
  resume_gaps (array of strings): supported facts the resume does not show,
  relevant_evidence_ids (array of ints from VERIFIED EVIDENCE),
  recommended_resume_focus (array of strings).""",
)


def build_ranking_prompt(candidate_profile: Any, resume: str, evidence: list[dict[str, Any]], job: Any) -> Prompt:
    user = user_message(
        RANKING_MARKER,
        section("CANDIDATE PROFILE", to_json(profile_facts(candidate_profile))),
        untrusted("RESUME", "untrusted_resume", resume, limit=16000),
        section("VERIFIED EVIDENCE", to_json(evidence, limit=30000)),
        section("JOB", to_json(job_facts(job))),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", getattr(job, "description", ""), limit=24000),
    )
    return Prompt("ranking", RANKING_PROMPT_VERSION, RANKING_MARKER, RANKING_SYSTEM, user)
