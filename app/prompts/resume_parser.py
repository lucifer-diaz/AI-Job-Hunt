"""Resume prompts: structured parsing of an uploaded resume, and evidence-claim extraction from the master resume."""
from __future__ import annotations

from app.prompts.common import (JSON_ONLY_RULE, NO_INVENTION_RULE, UNTRUSTED_DATA_RULE, Prompt, instructions,
                                untrusted, user_message)

RESUME_PARSER_PROMPT_VERSION = "v1"
RESUME_PARSER_MARKER = "PARSE_RESUME"
EVIDENCE_EXTRACTION_PROMPT_VERSION = "v1"
EVIDENCE_EXTRACTION_MARKER = "EXTRACT_EVIDENCE"

RESUME_PARSER_SYSTEM = instructions(
    "You convert the extracted text of an uploaded resume into structured data.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Preserve every fact exactly: do not rewrite, strengthen or summarise achievements. "
    "Clean extraction artifacts only (broken hyphenation, stray page numbers, headers and footers repeated "
    "on each page, bullet glyphs, duplicated whitespace). Use null for unknown values and [] for unknown lists.",
    JSON_ONLY_RULE,
    """Fields:
  name (string or null), headline (string or null),
  contact ({email, phone, location} strings or null, links (array of strings)),
  summary (string or null),
  skills (array of strings),
  experience (array of {title, company, location, start_date, end_date (strings or null), bullets (array of strings)}),
  education (array of {institution, degree, field, start_date, end_date (strings or null)}),
  certifications (array of strings), projects (array of {name, description (strings or null)}),
  clean_text (string): the full resume text with extraction artifacts removed and nothing else changed.""",
)

EVIDENCE_EXTRACTION_SYSTEM = instructions(
    "Extract concrete, factual achievement claims from the resume.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Never embellish a claim.",
    JSON_ONLY_RULE,
    "Fields: claims (array of {claim, company, role, skills (array)}).",
)


def build_resume_parser_prompt(resume_text: str) -> Prompt:
    user = user_message(RESUME_PARSER_MARKER, untrusted("RESUME TEXT", "untrusted_resume", resume_text, limit=40000))
    return Prompt("resume_parser", RESUME_PARSER_PROMPT_VERSION, RESUME_PARSER_MARKER, RESUME_PARSER_SYSTEM, user)


def build_evidence_extraction_prompt(resume_text: str) -> Prompt:
    # The mock provider reads the lines after "MASTER RESUME:"; keep that label.
    user = user_message(EVIDENCE_EXTRACTION_MARKER,
                        untrusted("MASTER RESUME", "untrusted_resume", resume_text, limit=22000).replace(
                            "MASTER RESUME (untrusted data):", "MASTER RESUME:", 1))
    return Prompt("evidence_extraction", EVIDENCE_EXTRACTION_PROMPT_VERSION, EVIDENCE_EXTRACTION_MARKER,
                  EVIDENCE_EXTRACTION_SYSTEM, user)
