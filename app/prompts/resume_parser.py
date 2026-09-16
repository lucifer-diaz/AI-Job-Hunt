"""Resume prompts: parsing an uploaded resume into clean raw_text and structured JSON, and evidence-claim extraction.

The parser prompt is not wired in yet: uploads are currently parsed deterministically (app.services.resume_format).
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

RESUME_PARSER_PROMPT_VERSION = "v2"
RESUME_PARSER_MARKER = "PARSE_RESUME"
EVIDENCE_EXTRACTION_PROMPT_VERSION = "v1"
EVIDENCE_EXTRACTION_MARKER = "EXTRACT_EVIDENCE"

# The shape of `structured_resume`. A list holding one object shows the shape of each item.
STRUCTURED_RESUME_TEMPLATE: dict[str, Any] = {
    "candidate": {"full_name": None, "headline": None, "location": None, "email": None, "phone": None,
                  "linkedin": None, "github": None, "portfolio": None, "other_links": []},
    "summary": None,
    "skills": {"ai_and_generative_ai": [], "backend_and_architecture": [], "cloud_and_devops": [],
               "data_and_streaming": [], "frontend_and_tools": [], "leadership": [], "other": []},
    "experience": [{
        "company": None, "job_title": None, "location": None, "employment_type": None,
        "start_date": None, "end_date": None, "is_current": False, "start_date_raw": None, "end_date_raw": None,
        "responsibilities": [], "achievements": [], "technologies": [], "skills": [],
        "metrics": [{"value": None, "unit": None, "description": None, "source_text": None}],
        "raw_bullets": [],
    }],
    "projects": [{"name": None, "description": None, "technologies": [], "skills": [], "achievements": [],
                  "raw_text": None}],
    "education": [{"degree": None, "field_of_study": None, "institution": None, "location": None,
                   "start_date": None, "end_date": None, "start_date_raw": None, "end_date_raw": None}],
    "certifications": [{"name": None, "issuer": None, "issue_date": None, "expiration_date": None,
                        "credential_id": None, "url": None}],
    "awards": [],
    "publications": [],
    "languages": [],
    "additional_information": [],
    "derived": {"total_experience_years": None, "current_role": None, "current_company": None,
                "primary_roles": [], "primary_skills": [], "leadership_experience": False, "ai_experience": False,
                "cloud_experience": False, "data_platform_experience": False},
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
- Always keep the original text in start_date_raw and end_date_raw.
- Current roles: "is_current": true and "end_date": null.
- Do not guess missing months or years: when a date cannot be normalized to YYYY-MM without guessing, set the
  normalized field to null and keep the original in the _raw field.

Experience
- Each distinct company and title period is a separate entry.
- Keep the original bullets in raw_bullets, then place each one, unchanged, in responsibilities or
  achievements. A bullet belongs in achievements when it shows measurable impact, scope, improvement,
  delivery, scale, leadership or a business outcome.

Skills
- Extract only skills explicitly present in the resume. Never infer skills from job titles alone.
- Normalize obvious naming variations (GCP -> Google Cloud Platform, K8s -> Kubernetes,
  Postgres -> PostgreSQL), but keep common canonical names such as AWS, FastAPI, LangGraph, OpenAI API, MCP
  and RAG.
- Technologies and skills on an experience entry must be explicitly mentioned in that role or its bullets.
  Do not copy every global skill into every job.

Metrics
- Extract each measurable outcome separately, for example
  {"value": "20", "unit": "x", "description": "latency reduction", "source_text": "..."}.
- Keep the exact wording in source_text. Do not reinterpret or recalculate metrics.
- If a role has no metrics, return an empty metrics list.

Derived fields
- Summarize only what the resume clearly supports.
- leadership_experience is true only when the resume explicitly shows leading teams, hiring, mentoring,
  performance management, roadmap ownership or technical leadership.
- ai_experience is true only when the resume explicitly mentions AI, ML, LLM or GenAI work.
- cloud_experience and data_platform_experience follow the same standard.
- total_experience_years: approximate total professional experience as a decimal rounded to one place,
  without double-counting overlapping roles; null when dates are too incomplete or ambiguous.

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
