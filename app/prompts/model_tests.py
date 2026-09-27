"""Small, safe prompts for Settings → AI Models: the model health test and each worker's configuration test.

Every input here is fictional fixture data, never a real candidate, job or email. The health test is a few dozen
tokens; the worker tests are short representative tasks with a small JSON schema, so "schema success" can be
checked without sending any of the account's data.
"""
from __future__ import annotations

from typing import Any

from app.prompts.common import JSON_ONLY_RULE, Prompt, instructions, section, to_json

MODEL_TEST_PROMPT_VERSION = "v1"
MODEL_TEST_MARKER = "MODEL_TEST"

MODEL_HEALTH_SYSTEM = instructions("You are a health check.", 'Return exactly this JSON object: {"ok": true}')

_CANDIDATE = {"name": "Sample Candidate", "headline": "Senior Backend Engineer",
              "skills": ["Python", "PostgreSQL", "Kubernetes"], "years_experience": 8}
_JOB = {"title": "Staff Platform Engineer", "company": "Example Corp", "location": "Remote",
        "requirements": ["Python", "Kubernetes", "Leading platform teams"]}

# worker type → (instruction, fixture data, expected output fields and their JSON types)
WORKER_TEST_TASKS: dict[str, tuple[str, dict[str, Any], dict[str, str]]] = {
    "RANKER": ("Score how well the job fits the candidate.",
               {"candidate": _CANDIDATE, "job": _JOB},
               {"overall_fit_score": "number", "confidence": "number", "reasoning_summary": "string"}),
    "RESUME_MATCHER": ("List which job requirements the candidate's skills satisfy.",
                       {"candidate": _CANDIDATE, "job": _JOB}, {"matched": "array", "missing": "array"}),
    "RESUME_PARSER": ("Extract the person's name and skills from the resume text.",
                      {"resume_text": "Sample Candidate\nSenior Backend Engineer\nSkills: Python, PostgreSQL"},
                      {"name": "string", "skills": "array"}),
    "JD_PARSER": ("Extract the job title and the list of requirements from the posting.",
                  {"posting": "Staff Platform Engineer at Example Corp. Requires Python and Kubernetes."},
                  {"title": "string", "requirements": "array"}),
    "TAILOR": ("Write a two-sentence professional summary for the candidate aimed at the job.",
               {"candidate": _CANDIDATE, "job": _JOB}, {"summary": "string"}),
    "REVIEWER": ("Review the tailored summary against the job and score it.",
                 {"job": _JOB, "summary": "Backend engineer with 8 years of Python and Kubernetes experience."},
                 {"score": "number", "reject_reasons": "array"}),
    "APPLICATION_ANSWER": ("Answer the application question in one sentence from the candidate facts.",
                           {"candidate": _CANDIDATE, "question": "Why are you interested in platform work?"},
                           {"answer": "string"}),
    "INBOX_CLASSIFIER": ("Classify the email as one of: interview, rejection, recruiter, other.",
                         {"subject": "Interview availability", "body": "Could you share times for a call next week?"},
                         {"category": "string", "confidence": "number"}),
    "FOLLOWUP": ("Draft a short, polite follow-up note about an application.",
                 {"job": _JOB, "days_since_applied": 7}, {"draft": "string"}),
    "COMPANY_INSIGHTS": ("Summarise what the fictional company does in one sentence.",
                         {"company": "Example Corp", "notes": "Builds developer tooling for logistics firms."},
                         {"summary": "string"}),
    "INTERVIEW_INSIGHTS": ("List two topics to prepare for this interview.",
                           {"job": _JOB}, {"topics": "array"}),
    "AI_OPTIMIZATION": ("Give one recommendation from the usage figures.",
                        {"requests": 120, "failures": 3, "p95_latency_ms": 9000}, {"recommendation": "string"}),
    "JOB_NORMALIZATION": ("Normalise the job's location and seniority.",
                          {"title": "Sr. Staff Eng.", "location": "remote - US"},
                          {"location": "string", "seniority": "string"}),
    "EVIDENCE_EXTRACTION": ("Extract verifiable claims (with a number) from the resume lines.",
                            {"lines": ["Cut API latency by 40% across 12 services", "Enjoys hiking"]},
                            {"claims": "array"}),
}


def build_model_health_prompt() -> Prompt:
    return Prompt("model_health", MODEL_TEST_PROMPT_VERSION, MODEL_TEST_MARKER, MODEL_HEALTH_SYSTEM,
                  f"{MODEL_TEST_MARKER}\n{section('TASK', 'Health check')}")


def build_worker_test_prompt(worker_type: str) -> tuple[Prompt, dict[str, str]]:
    """A fixture task representative of the worker, and the fields its JSON answer must have."""
    instruction, data, expected = WORKER_TEST_TASKS.get(worker_type) or (
        "Summarise the input in one sentence.", {"text": "A fictional configuration test."}, {"summary": "string"})
    fields = ", ".join(f"{name} ({kind})" for name, kind in expected.items())
    system = instructions(f"This is a configuration test with fictional data. {instruction}",
                          f"Return JSON with exactly these fields: {fields}.", JSON_ONLY_RULE)
    user = f"{MODEL_TEST_MARKER}\n{section('FIXTURE', to_json(data))}"
    return Prompt("worker_test", MODEL_TEST_PROMPT_VERSION, MODEL_TEST_MARKER, system, user), expected
