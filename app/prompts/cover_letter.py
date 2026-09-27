"""Cover letter: job + company + verified facts + the candidate's own knowledge answers → a letter they could send.

The letter is written in the candidate's voice ("I"), from verified facts only. It is generated apart from the
resume so that the knowledge base (notice period, work authorization, relocation) reaches the letter and never
the resume rewriting, and so the letter gets a prompt of its own instead of one line in a larger schema.
Output contract: app.prompts.contracts.CoverLetterResult.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import (
    GAPS_STAY_GAPS_RULE,
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    compact,
    context_value,
    instructions,
    resume_facts,
    section,
    to_json,
    untrusted,
    user_message,
)
from app.prompts.contracts import CoverLetterResult, describe_fields

# v4: verified resume facts are application data, not untrusted text; each fact is sent once.
# v3: about a third shorter (one shape paragraph, one avoid list), no personal name in the instructions.
# v2: typed output contract; gaps stay gaps; only the candidate fields a letter uses; the FitAnalysis only when the
# Ranker completed one; the candidate policy applied before the prompt instead of sent for the model to reason about.
COVER_LETTER_PROMPT_VERSION = "v4"
COVER_LETTER_MARKER = "COVER_LETTER"
CANDIDATE_FIELDS = ("name", "headline", "location", "work_authorization")

COVER_LETTER_SYSTEM = instructions(
    "You write one cover letter, as the candidate, for the job in this message.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Every fact, number and outcome comes from VERIFIED RESUME FACTS, VERIFIED EVIDENCE or "
    "KNOWN ANSWERS, and everything about the company from JOB and JOB DESCRIPTION. The writing can be natural; the "
    "truth constraints do not relax.",
    GAPS_STAY_GAPS_RULE,
    """Write in the first person ("I led", "my team"): never the third person, never the candidate's name in the
body. One honest letter, not a profile.
SHAPE: "Dear Hiring Team," (or the first recruiter in RECRUITERS by name). Paragraph 1: the exact role title and a
concrete reason from the posting. Paragraphs 2-3: the two strongest proofs for this posting's requirements, each with
its real number or scope, in fresh words rather than resume bullets. Paragraph 4: what they bring, anything from KNOWN
ANSWERS the posting raises (work authorization, notice period, relocation, remote), a plain close. Then "Sincerely,"
and the name from CANDIDATE PROFILE. 250-300 words, plain ASCII, no markdown, lists, headings, em dashes or
placeholders such as [Company].
AVOID: "passionate about", "thrilled", "I am writing to express my interest" and other cliches; listing technologies;
company claims the posting does not make; salary or visa details it never raises; drawing attention to anything the
candidate lacks.""",
    JSON_ONLY_RULE,
    describe_fields(CoverLetterResult),
)


def build_cover_letter_prompt(context: Any, recruiters: list[dict[str, str]] | None = None) -> Prompt:
    """Build from a cover letter context (app.services.context_builder.build_cover_letter_context)."""
    candidate = context_value(context, "candidate", {}) or {}
    job = context_value(context, "job", {}) or {}
    knowledge = context_value(context, "knowledge", [])
    fit = compact({key: (context_value(context, "fit_analysis") or {}).get(key)
                   for key in ("strong_matches", "recommended_resume_focus")})
    user = user_message(
        COVER_LETTER_MARKER,
        section("CANDIDATE PROFILE", to_json(compact({k: candidate.get(k) for k in CANDIDATE_FIELDS}))),
        section("VERIFIED RESUME FACTS", to_json(resume_facts(context), limit=16000)),
        section("VERIFIED EVIDENCE", to_json(context_value(context, "relevant_evidence", []), limit=16000)),
        section("KNOWN ANSWERS", to_json(knowledge)) if knowledge else "",
        section("JOB", to_json({k: v for k, v in job.items() if k != "description"})),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", job.get("description", ""), limit=24000),
        section("FIT ANALYSIS", to_json(fit)) if fit else "",
        section("RECRUITERS (the first is the primary contact)", to_json(recruiters)) if recruiters else "",
    )
    return Prompt("cover_letter", COVER_LETTER_PROMPT_VERSION, COVER_LETTER_MARKER, COVER_LETTER_SYSTEM, user,
                  CoverLetterResult)
