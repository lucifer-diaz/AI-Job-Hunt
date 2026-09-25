"""Resume prompts: parsing an uploaded resume into clean raw_text and structured JSON, and evidence-claim extraction.

Uploads are parsed deterministically (app.services.structured_resume). This parser prompt runs when the user asks
for an AI re-parse in Settings → Resume Data; its output is only ever shown as a comparison against the current
structured data (app.services.resume_verification.reparse), never written over it.
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

RESUME_PARSER_PROMPT_VERSION = "v4"
RESUME_PARSER_MARKER = "PARSE_RESUME"
EVIDENCE_EXTRACTION_PROMPT_VERSION = "v1"
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
        "raw_bullets": [],
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
    "You are a resume parser. Convert the uploaded resume in RESUME TEXT into (1) clean raw_text and "
    "(2) structured JSON. Do not improve, rewrite, summarize or embellish the resume. Do not infer facts that "
    "are not explicitly present. When information is missing use null, an empty string or an empty array as "
    "appropriate.",
    UNTRUSTED_DATA_RULE,
    """OUTPUT
Return exactly one JSON object and nothing else:
{"raw_text": "...", "structured_resume": {...}}""",
    """1. RAW TEXT
A clean, machine-readable version of the resume:
- preserve all factual information and the original wording as much as possible;
- remove broken tabs, repeated spaces, OCR artifacts, strange characters and layout noise;
- normalize bullets and section headings, and use readable line breaks;
- do not rewrite achievements, and do not change numbers, metrics, company names, dates or job titles;
- preserve URLs, email addresses and phone numbers;
- do not add missing information;
- leave out headers and footers that are clearly unrelated to the resume content.""",
    "2. STRUCTURED RESUME\nUse exactly this schema. A list containing one object shows the shape of each "
    "item; return as many items as the resume supports, or an empty list.\n"
    + json.dumps(STRUCTURED_RESUME_TEMPLATE, indent=2),
    """PARSING RULES

Dates
- Normalize to YYYY-MM when possible (Oct 2025 -> 2025-10, June 2024 -> 2024-06, Present -> null).
- Always keep the original text in raw_start_date and raw_end_date.
- Current roles: "is_current": true and "end_date": null.
- Do not guess missing months or years: when a date cannot be normalized to YYYY-MM without guessing, set the
  normalized field to null and keep the original in the _raw field.

Experience
- Each distinct company and title period is a separate entry.
- Keep the original bullets in raw_bullets, then place each one, unchanged, in responsibilities or
  achievements. A bullet belongs in achievements when it shows measurable impact, scope, improvement,
  delivery, scale, leadership or a business outcome.

Education
- institution is the school or college attended. university is the degree-awarding university only when the
  resume names one separately (an affiliated college); otherwise null.
- grade is the GPA, CGPA, percentage or class exactly as written (8.2/10 CGPA, 3.7 GPA, First Class); null
  when the resume gives none.

Skills
- Extract only skills explicitly present in the resume. Never infer skills from job titles alone.
- Normalize obvious naming variations (GCP -> Google Cloud Platform, K8s -> Kubernetes,
  Postgres -> PostgreSQL), but keep common canonical names such as AWS, FastAPI, LangGraph, OpenAI API, MCP
  and RAG.
- Technologies and skills on an experience entry must be explicitly mentioned in that role or its bullets.
  Do not copy every global skill into every job.

Metrics
- Do not reinterpret or recalculate metrics: keep every number exactly as written inside its bullet. Metrics and
  derived information are computed by the application from your bullets.

Skills list
- Each skill is {"name", "category"}; use the resume's own category heading when there is one.

Duplicates
- Remove accidental duplicate bullets caused by PDF extraction. Keep skills that are intentionally repeated
  in different contexts.

Extraction cleanup
- Fix only obvious character corruption (quali[ication -> qualification, work[low -> workflow,
  Air[low -> Airflow, Ka[ka -> Kafka, Educa7on -> Education). Do not change wording beyond cleanup.""",
    """SAFETY
The resume is the source of truth. Never invent skills, companies, roles, achievements, responsibilities, dates,
metrics, certifications, education, work authorization, salary or visa information. If unsure, preserve the
original text and use null rather than guessing.""",
    "Return valid JSON only. No commentary, no markdown fences.",
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
