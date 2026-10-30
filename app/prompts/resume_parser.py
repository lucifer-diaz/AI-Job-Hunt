"""Resume prompts: parsing an uploaded resume into structured JSON, and evidence-claim extraction.

Uploads are parsed deterministically at once (app.services.structured_resume); this parser prompt then runs in the
background for every new version, and when the user asks for an AI re-parse (app.services.resume_parsing). Its output
is held to the resume's own text in code (`resume_parsing.ground`): a value the text does not contain is dropped. It
fills the structured data only while nobody has reviewed it, and is a comparison otherwise.
"""
from __future__ import annotations

import json
from typing import Any

from app.prompts.common import (
    JSON_ONLY_RULE,
    NO_INVENTION_RULE,
    UNTRUSTED_DATA_RULE,
    Prompt,
    instructions,
    untrusted,
    user_message,
)
from app.prompts.contracts import EvidenceExtractionResult, ResumeParserOutput, describe_fields

# v5: v4 (main: university and grade on education entries) merged with the trimmed parser: structured JSON
# only. The cleaned raw_text copy and raw_bullets were never read (the application keeps the uploaded text
# and derives raw_bullets itself), so the model no longer writes the resume two or three times.
# v6: header lines in any order (company first, title first, a line each, a location or dates between), several
# titles at one employer, and two-column PDF text; every value copied as written, since code checks it against the text.
RESUME_PARSER_PROMPT_VERSION = "v6"
RESUME_PARSER_MARKER = "PARSE_RESUME"
# v2: typed output contract; a claim keeps the employer and role it sits under in the resume, never another one.
EVIDENCE_EXTRACTION_PROMPT_VERSION = "v2"
EVIDENCE_EXTRACTION_MARKER = "EXTRACT_EVIDENCE"

# The shape of `structured_resume`. A list holding one object shows the shape of each item.
STRUCTURED_RESUME_TEMPLATE: dict[str, Any] = {
    "personal_information": {"full_name": None, "headline": None, "location": None, "email": None, "phone": None,
                             "linkedin": None, "github": None, "portfolio": None, "other_links": []},
    "summary": None,
    "skills": [{"name": None, "category": None}],
    "experience": [{
        "company": None, "job_title": None, "location": None, "employment_type": None,
        "start_date": None, "end_date": None, "is_current": False, "raw_start_date": None, "raw_end_date": None,
        "responsibilities": [], "achievements": [], "technologies": [], "skills": [],
    }],
    "projects": [{"name": None, "description": None, "url": None, "technologies": [], "skills": [], "achievements": []}],
    "education": [{"degree": None, "field_of_study": None, "institution": None, "university": None,
                   "location": None, "grade": None, "start_date": None, "end_date": None, "raw_start_date": None, "raw_end_date": None}],
    "certifications": [{"name": None, "issuer": None, "issue_date": None, "expiration_date": None,
                        "credential_id": None, "url": None}],
    "awards": [{"title": None, "issuer": None, "date": None, "description": None}],
    "languages": [{"name": None, "proficiency": None}],
    "additional_information": [],
}

RESUME_PARSER_SYSTEM = instructions(
    "You are a resume parser. Convert RESUME TEXT into structured JSON. You extract; you never improve, rewrite, "
    "summarize or embellish, and you never infer what the resume does not state.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Never add work authorization, salary or visa information. Missing values are null, an "
    "empty string or an empty list.",
    'Return one JSON object: {"structured_resume": {...}} using exactly this schema (a list holding one object '
    "shows the shape of each item; return as many items as the resume supports, or an empty list):\n"
    + json.dumps(STRUCTURED_RESUME_TEMPLATE, separators=(",", ":")),
    """RULES
Dates: YYYY-MM when the text allows it (Oct 2025 -> 2025-10, Present -> null with "is_current": true). Always copy
  the original into raw_start_date / raw_end_date. Never guess a missing month or year: leave the normalized field
  null.
Experience: one entry per company and title period. A role's header may name the company first or the title
  first, on one line or two, separated by |, -, comma, "at" or a line break, with the location and dates anywhere in
  it. Tell them apart by meaning: job_title is the position held (Senior Software Engineer, Lead AI Engineer,
  Intern), company is the employer (Zendrive, Google, Acme Labs); a place (Bangalore, India, Remote, US) is
  location, never company or title. Several titles at one employer are separate entries with the same company.
  Put every bullet, word for word, in achievements (measurable impact, scope, delivery, leadership, outcomes) or
  responsibilities (everything else). Keep numbers exactly as written.
Copy, never paraphrase: every name, title, bullet and date is checked against the resume text, and a value that is
  not in it is thrown away. Do not expand abbreviations (Sr. stays Sr.), merge bullets or complete a sentence.
Columns: text from a two-column PDF may interleave a sidebar (skills, contact, education) with the experience
  bullets. Put each line in the section it belongs to; a sidebar line is never a bullet of a role.
Skills: only skills the resume names; never infer them from a job title. Normalize obvious variants (K8s ->
  Kubernetes, Postgres -> PostgreSQL, GCP -> Google Cloud Platform); keep canonical names such as AWS, FastAPI,
  LangGraph, OpenAI API, MCP and RAG. Use the resume's own category heading. A role's skills and technologies are
  only those named in that role.
Education: institution is the school or college attended; university is the degree-awarding university only when
  the resume names one separately (an affiliated college), otherwise null. grade is the GPA, CGPA, percentage or
  class exactly as written (8.2/10 CGPA, First Class), or null.
Cleanup: drop duplicate bullets caused by PDF extraction, and fix only obvious character corruption
  (quali[ication -> qualification, Ka[ka -> Kafka, Educa7on -> Education).""",
    JSON_ONLY_RULE,
)

EVIDENCE_EXTRACTION_SYSTEM = instructions(
    "You extract concrete, factual achievement claims from the resume. You extract; you never improve, merge or "
    "reword a claim.",
    UNTRUSTED_DATA_RULE,
    NO_INVENTION_RULE + " Never embellish a claim.",
    "company and role are the employer and job title the resume places the claim under. When the resume does not "
    "make that clear, leave them empty rather than guessing; never attach a claim to another employer or role.",
    JSON_ONLY_RULE,
    describe_fields(EvidenceExtractionResult),
)


def build_resume_parser_prompt(resume_text: str) -> Prompt:
    user = user_message(RESUME_PARSER_MARKER, untrusted("RESUME TEXT", "untrusted_resume", resume_text, limit=40000))
    return Prompt("resume_parser", RESUME_PARSER_PROMPT_VERSION, RESUME_PARSER_MARKER, RESUME_PARSER_SYSTEM, user,
                  ResumeParserOutput)


def build_evidence_extraction_prompt(resume_text: str) -> Prompt:
    # The mock provider reads the lines after "MASTER RESUME:"; keep that label.
    user = user_message(EVIDENCE_EXTRACTION_MARKER,
                        untrusted("MASTER RESUME", "untrusted_resume", resume_text, limit=22000).replace(
                            "MASTER RESUME (untrusted data):", "MASTER RESUME:", 1))
    return Prompt("evidence_extraction", EVIDENCE_EXTRACTION_PROMPT_VERSION, EVIDENCE_EXTRACTION_MARKER,
                  EVIDENCE_EXTRACTION_SYSTEM, user, EvidenceExtractionResult)
