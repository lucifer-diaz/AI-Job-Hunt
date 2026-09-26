"""Tailor: source resume + completed FitAnalysis + structured job + relevant verified evidence → a tailoring patch.

The Tailor presents; it does not judge. The Ranker has already computed the FitAnalysis, and the Tailor receives it
as read-only input — never as a template with empty fields to fill. The answer is a patch (app.prompts.contracts.
TailoringResult) that a deterministic renderer applies to the source resume, so roles, employers, titles and dates
can never be removed or changed.

What reaches this prompt was filtered by app.services.context_builder.build_tailoring_context: only facts the
CandidatePolicy allows, only evidence relevant to this job, one canonical copy of each fact.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    GAPS_STAY_GAPS_RULE,
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    REQUIREMENT_LEVELS_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    candidate_identity,
    compact,
    context_value,
    instructions,
    job_facts,
    job_sections,
    job_structured,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import TailoringResult, describe_fields

# v5: patch output with evidence ids per claim; completed FitAnalysis as read-only input (no score, no empty fields);
# one canonical source resume instead of roles + resume text + policy; candidate context is name and headline only.
TAILOR_PROMPT_VERSION = "v5"
TAILOR_MARKER = "TAILOR_RESUME"
# The FitAnalysis fields the Tailor reads. Scores are deliberately absent: the Tailor does not work with them.
FIT_ANALYSIS_FIELDS = ("strong_matches", "partial_matches", "resume_gaps", "recommended_resume_focus")
# The Ranker's gaps, sent only so the Tailor knows what it must not claim.
GAP_FIELDS = ("missing_requirements", "fatal_gaps")
# Section labels in the order they appear; the tests hold the prompt to them.
SECTIONS = ("CANDIDATE", "SOURCE RESUME", "RELEVANT EVIDENCE", "JOB", "JOB REQUIREMENTS", "FIT ANALYSIS")

TAILOR_SYSTEM = instructions(
    "You are the resume-tailoring component of Job Hunt OS. Your only task is to improve how SOURCE RESUME presents "
    "the candidate's verified experience for this job.",
    "You are not the Ranker. FIT ANALYSIS has already been produced by the Ranker: do not alter or recompute it, "
    "do not calculate fit or scores, and do not decide whether the candidate should apply. Use its strong_matches, "
    "partial_matches, resume_gaps and recommended_resume_focus to choose what to emphasise.",
    NO_INVENTION_RULE + " SOURCE RESUME and RELEVANT EVIDENCE are the only facts you may use. Do not change "
    "employers, titles or dates, and do not reclassify job requirements. " + GAPS_STAY_GAPS_RULE
    + " FIT ANALYSIS.gaps_to_preserve lists requirements the candidate's facts do not cover.",
    """You may: rewrite the summary; order skills_order using only skills in SOURCE RESUME; for a role, select and
rewrite bullets using that role's own bullets and RELEVANT EVIDENCE from the same employer, one achievement per
bullet, most relevant first. Transferable experience may be emphasised; it is never renamed as the missing skill.
Change only what helps this job: a role you leave out of role_updates keeps its source bullets unchanged, and a role
you include shows exactly the 1-5 bullets you return. Update at most 3 roles.
Every summary and bullet lists in evidence_ids the ids (from SOURCE RESUME bullets or RELEVANT EVIDENCE) it rests on,
and uses only numbers that appear in what it cites.""",
    UNTRUSTED_DATA_RULE,
    REQUIREMENT_LEVELS_RULE,
    JSON_ONLY_RULE + " Return only the TailoringResult object.",
    describe_fields(TailoringResult),
    "Use plain ASCII punctuation (- for dashes, x for multipliers). Keep bullets concise and ATS-friendly.",
)


class TailorPromptError(ValueError):
    """The prompt cannot be built from this context. Raised before any model call."""


def fit_analysis_input(analysis: dict[str, Any] | None) -> dict[str, Any]:
    """The completed FitAnalysis as the Tailor reads it: present, non-empty fields only, gaps kept apart."""
    analysis = analysis or {}
    view = compact({key: analysis.get(key) for key in FIT_ANALYSIS_FIELDS})
    gaps = [item for key in GAP_FIELDS for item in analysis.get(key) or []]
    if gaps:
        view["gaps_to_preserve"] = list(dict.fromkeys(gaps))
    return view


def build_tailoring_prompt(candidate_profile: Any, job: Any, context: Any) -> Prompt:
    """Render a tailoring context (app.services.context_builder.build_tailoring_context). The context has already
    been validated; a context without a source resume or a completed FitAnalysis is refused, never padded."""
    source = context_value(context, "source_resume") or {}
    analysis = context_value(context, "fit_analysis")
    if not source and not context_value(context, "relevant_evidence", []):
        raise TailorPromptError("TAILOR_CONTEXT_INCOMPLETE: there is no source resume or evidence to tailor from")
    if not analysis:
        raise TailorPromptError("RANKING_REQUIRED: the Tailor needs the Ranker's completed FitAnalysis")
    user = user_message(
        TAILOR_MARKER,
        section("CANDIDATE", to_json(candidate_identity(candidate_profile))),
        section("SOURCE RESUME", to_json(source, limit=24000)),
        section("RELEVANT EVIDENCE", to_json(context_value(context, "relevant_evidence", []), limit=24000)),
        section("JOB", to_json(job_facts(job))),
        *job_sections(context, fields=("identity", "role_summary", "required", "preferred", "responsibilities",
                                       "leadership_expectations", "technical_domains")),
        "" if job_structured(context) else untrusted("JOB DESCRIPTION", "untrusted_job_description",
                                                     getattr(job, "description", ""), limit=20000),
        section("FIT ANALYSIS (completed by the Ranker; read-only)", to_json(fit_analysis_input(analysis))),
    )
    return Prompt("tailoring", TAILOR_PROMPT_VERSION, TAILOR_MARKER, TAILOR_SYSTEM, user, TailoringResult)
