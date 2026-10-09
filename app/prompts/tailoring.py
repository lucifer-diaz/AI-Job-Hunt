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
    posting_text,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import TailoringResult, describe_fields

# v7: a page target and a bullet budget per role (LENGTH PLAN): the source resume is the foundation to select from,
# not a checklist; every budgeted role is returned with at most its max_bullets, and a role with 0 keeps its heading.
# v6: evidence a source bullet already states is not repeated in RELEVANT EVIDENCE, and the posting arrives
# without duplicate paragraphs or legal boilerplate.
# v5: patch output with evidence ids per claim; completed FitAnalysis as read-only input (no score, no empty fields);
# one canonical source resume instead of roles + resume text + policy; candidate context is name and headline only.
TAILOR_PROMPT_VERSION = "v7"
TAILOR_MARKER = "TAILOR_RESUME"
# The FitAnalysis fields the Tailor reads. Scores are deliberately absent: the Tailor does not work with them.
FIT_ANALYSIS_FIELDS = ("strong_matches", "partial_matches", "resume_gaps", "recommended_resume_focus")
# The Ranker's gaps, sent only so the Tailor knows what it must not claim.
GAP_FIELDS = ("missing_requirements", "fatal_gaps")
# Section labels in the order they appear; the tests hold the prompt to them.
SECTIONS = ("CANDIDATE", "SOURCE RESUME", "RELEVANT EVIDENCE", "JOB", "JOB REQUIREMENTS", "LENGTH PLAN", "FIT ANALYSIS")

TAILOR_SYSTEM = instructions(
    "You are the resume-tailoring component of Job Hunt OS. Your only task is to improve how SOURCE RESUME presents "
    "the candidate's verified experience for this job.",
    "You are not the Ranker. FIT ANALYSIS has already been produced by the Ranker: do not alter or recompute it, "
    "do not calculate fit or scores, and do not decide whether the candidate should apply. Use its strong_matches, "
    "partial_matches, resume_gaps and recommended_resume_focus to choose what to emphasise.",
    NO_INVENTION_RULE + " SOURCE RESUME and RELEVANT EVIDENCE are the only facts you may use. Do not change "
    "employers, titles or dates, and do not reclassify job requirements. " + GAPS_STAY_GAPS_RULE
    + " FIT ANALYSIS.gaps_to_preserve lists requirements the candidate's facts do not cover.",
    """SOURCE RESUME is the foundation you build this resume from, not a checklist: its bullets are candidates.
For each role, select only the bullets that best serve this job, rewrite them, and leave the rest out.
LENGTH PLAN gives the page target and each role's max_bullets, newest role first: the most recent roles carry the
most detail and older roles less. Return a role_update for every role whose max_bullets is above 0, with at most
max_bullets bullets, most relevant first. Fewer is fine when a role has little that is relevant; never pad, and
never repeat one achievement in two bullets. A role with max_bullets 0 keeps its title, employer and dates with no
bullets: return no bullets for it.
You may also: rewrite the summary (at most LENGTH PLAN.summary_sentences sentences); order skills_order using only
skills in SOURCE RESUME. A role's bullets use that role's own bullets and RELEVANT EVIDENCE from the same employer,
one achievement per bullet. Transferable experience may be emphasised; it is never renamed as the missing skill.
Every summary and bullet lists in evidence_ids the ids it rests on: the id of a SOURCE RESUME bullet or project
bullet, or of RELEVANT EVIDENCE (verified facts that are not on the resume),
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


def length_plan_input(plan: dict[str, Any] | None) -> dict[str, Any]:
    """The length plan as the Tailor reads it: the page target, the summary's sentence budget and each role's
    max_bullets by role_index (computed by app.services.resume_length; this module only renders it)."""
    plan = plan or {}
    return {"target": plan.get("target") or "2 pages", "summary_sentences": plan.get("summary_sentences") or 4,
            "max_bullets_by_role_index": {int(k): v for k, v in (plan.get("role_caps") or {}).items()}}


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
                                                     posting_text(context, job), limit=20000),
        section("LENGTH PLAN", to_json(length_plan_input(context_value(context, "length_plan")))),
        section("FIT ANALYSIS (completed by the Ranker; read-only)", to_json(fit_analysis_input(analysis))),
    )
    return Prompt("tailoring", TAILOR_PROMPT_VERSION, TAILOR_MARKER, TAILOR_SYSTEM, user, TailoringResult)
