"""Resume tailoring: job + CandidateProfile + resume + fit analysis + verified Evidence → tailored packet (stage: tailor)."""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    UNTRUSTED_DATA_RULE,
    UNVERIFIED_FACTS_RULE,
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

TAILOR_PROMPT_VERSION = "v3"
TAILOR_MARKER = "TAILOR_RESUME"
# The fit-analysis fields tailoring uses, with their defaults; anything else stored with the analysis is left out.
# resume_fit_score, resume_gaps and recommended_resume_focus come from the ranker and say what the resume under-sells.
FIT_ANALYSIS_DEFAULTS = {"summary": "", "strong_matches": [], "gaps": [], "must_have_missing": [], "keywords": [],
                         "application_strategy": "", "confidence": 0, "candidate_fit_score": None,
                         "resume_fit_score": None, "resume_gaps": [], "recommended_resume_focus": []}

TAILOR_SYSTEM = instructions(
    "You are an expert resume editor.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Tailor only from facts in MASTER RESUME ROLES, RESUME and VERIFIED EVIDENCE. Every "
    "factual claim must be supported by one of them; list the evidence you relied on in selected_evidence_ids. "
    "You may reorder, select and rewrite how facts are presented, but not change what they say. Prefer "
    "quantified evidence relevant to the job. You produce a new version: the master resume itself is never changed.",
    "VERIFIED RESUME FACTS and the bullets in MASTER RESUME ROLES contain only facts the candidate verified. Use only "
    "those and VERIFIED EVIDENCE for factual claims (employers, titles, dates, metrics, skills, achievements). A "
    "bullet with \"type\": \"unverified\" appears only because CANDIDATE POLICY allows unverified facts. Never add "
    "a metric, employer, skill or date that is not in them. Roles without bullets keep their existing bullets.",
    UNVERIFIED_FACTS_RULE,
    JSON_ONLY_RULE,
    """Fields:
  summary (string),
  skills (array of skills copied from the master resume, most relevant first),
  experience (array of {role_index (int), bullets (array of strings)}): rewrite bullets for the roles in
    MASTER RESUME ROLES. Each role's bullets may only use that role's own facts or verified evidence for
    the same company. Keep 1-5 bullets per role, one achievement per bullet, most relevant first.
    Titles, companies, locations and dates are kept automatically; do not repeat them.
  experience_bullets (array of strings): only when MASTER RESUME ROLES is empty,
  selected_evidence_ids (array of ints).
The cover letter is written separately: do not produce one here.""",
    "Use plain ASCII punctuation (- for dashes, x for multipliers). Keep bullets concise and ATS-friendly.",
)


def build_tailoring_prompt(candidate_profile: Any, resume: str = "", roles: list[dict[str, Any]] | None = None,
                           evidence: list[dict[str, Any]] | None = None, job: Any = None,
                           fit_analysis: dict[str, Any] | None = None, fit_score: float | None = None,
                           context: Any = None) -> Prompt:
    """With a tailoring context (app.services.context_builder.build_tailoring_context) the roles, resume facts and
    evidence come from it: verified facts relevant to this job only."""
    analysis = fit_analysis or (context_value(context, "fit_analysis") if context is not None else None) or {}
    fit = {"score": fit_score, **{key: analysis.get(key, default) for key, default in FIT_ANALYSIS_DEFAULTS.items()}}
    if context is not None:
        roles = [{k: v for k, v in r.items() if k != "verified"} for r in context_value(context, "roles", [])]
        resume_section = untrusted("VERIFIED RESUME FACTS", "untrusted_resume_facts",
                                   context_value(context, "resume_text", ""), limit=22000)
        evidence = context_value(context, "relevant_evidence", [])
        policy = section("CANDIDATE POLICY", to_json(context_value(context, "policy", {})))
    else:
        resume_section, policy = untrusted("RESUME", "untrusted_resume", resume, limit=22000), ""
    user = user_message(
        TAILOR_MARKER,
        section("MASTER RESUME ROLES", to_json(roles or [], limit=20000)),
        section("CANDIDATE PROFILE", to_json(profile_facts(candidate_profile, include_name=True))),
        resume_section,
        section("VERIFIED EVIDENCE", to_json(evidence or [], limit=32000)),
        policy,
        section("JOB", to_json(job_facts(job))),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", getattr(job, "description", ""), limit=26000),
        section("FIT ANALYSIS", to_json(fit)),
    )
    return Prompt("tailoring", TAILOR_PROMPT_VERSION, TAILOR_MARKER, TAILOR_SYSTEM, user)
