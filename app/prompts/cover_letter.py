"""Cover letter: job + company + verified facts + the candidate's own knowledge answers → a letter they could send.

The letter is written in the candidate's voice ("I"), from verified facts only. It is generated apart from the
resume so that the knowledge base (notice period, work authorization, relocation) reaches the letter and never
the resume rewriting, and so the letter gets a prompt of its own instead of one line in a larger schema.
"""
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
    resume_facts,
    section,
    to_json,
    untrusted,
    user_message,
)

COVER_LETTER_PROMPT_VERSION = "v1"
COVER_LETTER_MARKER = "COVER_LETTER"

COVER_LETTER_SYSTEM = instructions(
    "You write one cover letter, as the candidate, for the job in this message.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Every fact, employer, title, number and outcome must come from VERIFIED RESUME FACTS, "
    "VERIFIED EVIDENCE or KNOWN ANSWERS. Never invent facts about the company either: say only what the JOB "
    "DESCRIPTION and JOB state.",
    UNVERIFIED_FACTS_RULE,
    """VOICE
Write in the first person, as the candidate: "I led", "I cut", "my team". Never write about them in the third
person, never use their name in the body, and never describe them from outside ("Rohan Roy is an engineering
leader with 11 years..."). Sound like a capable person writing one honest letter, not a profile or a brochure.""",
    """SHAPE
  Line 1: "Dear Hiring Team," (use the recruiter's name from RECRUITERS when one is given).
  Paragraph 1 (2-3 sentences): the role by its exact title and why this company and this work, using something
    concrete from the posting. No "I am writing to express my interest".
  Paragraph 2 and 3 (3-4 sentences each): the two strongest proofs for THIS posting's requirements, each with
    the real number or scope from the verified facts, in the candidate's own words rather than resume wording.
  Final paragraph (2-3 sentences): what they would bring to this team, anything from KNOWN ANSWERS the posting
    actually raises (work authorization, notice period, relocation, remote working), and a plain closing line.
  Then "Sincerely," on its own line and the candidate's name from CANDIDATE PROFILE on the next.
250-300 words in total, four short paragraphs. Plain ASCII, no markdown, no bullet lists, no headings, no
placeholders such as [Company] or [Role], no em dashes.""",
    """AVOID
  - flattery and cliches: "passionate about", "thrilled", "world-class", "I would love the opportunity",
  - repeating the resume bullet for bullet, or listing technologies for their own sake,
  - claims about the company's culture, funding, mission or size that the posting does not state,
  - salary, visa or availability details the posting never raises,
  - apologising for, or drawing attention to, anything the candidate lacks.""",
    JSON_ONLY_RULE,
    """Fields:
  cover_letter (string): the whole letter, with "\\n\\n" between paragraphs,
  selected_evidence_ids (array of ints): the VERIFIED EVIDENCE ids the letter relies on, if any.""",
)


def build_cover_letter_prompt(context: Any, recruiters: list[dict[str, str]] | None = None) -> Prompt:
    """Build from a cover letter context (app.services.context_builder.build_cover_letter_context)."""
    candidate = context_value(context, "candidate", {}) or {}
    job = context_value(context, "job", {}) or {}
    knowledge = context_value(context, "knowledge", [])
    fit = context_value(context, "fit_analysis") or {}
    user = user_message(
        COVER_LETTER_MARKER,
        section("CANDIDATE PROFILE", to_json({k: v for k, v in candidate.items() if k != "verified_background"})),
        untrusted("VERIFIED RESUME FACTS", "untrusted_resume_facts", to_json(resume_facts(context), limit=16000)),
        section("VERIFIED EVIDENCE", to_json(context_value(context, "relevant_evidence", []), limit=16000)),
        section("KNOWN ANSWERS", to_json(knowledge)) if knowledge else "",
        section("JOB", to_json({k: v for k, v in job.items() if k != "description"})),
        untrusted("JOB DESCRIPTION", "untrusted_job_description", job.get("description", ""), limit=24000),
        section("FIT ANALYSIS", to_json({key: fit.get(key) for key in ("strong_matches", "keywords",
                                                                       "application_strategy") if fit.get(key)}))
        if fit else "",
        section("RECRUITERS (the first is the primary contact)", to_json(recruiters)) if recruiters else "",
        section("CANDIDATE POLICY", to_json(context_value(context, "policy", {}))),
    )
    return Prompt("cover_letter", COVER_LETTER_PROMPT_VERSION, COVER_LETTER_MARKER, COVER_LETTER_SYSTEM, user)
