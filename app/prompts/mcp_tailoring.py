"""Claude Desktop bulk tailoring over MCP: the rules sent with tailoring context and the `tailor_jobs` MCP prompt.

Claude Desktop does the reasoning here, so these are instructions for a conversation, not a model call the
backend makes. Job Hunt OS still validates every saved result (app.services.tailoring_results).
"""
from __future__ import annotations

from app.prompts.common import (
    GAPS_STAY_GAPS_RULE,
    NO_INVENTION_RULE,
    UNTRUSTED_DATA_RULE,
    instructions,
)

# v2: tailoring is told it is not ranking, and that the fit analysis's gaps stay gaps.
MCP_TAILORING_PROMPT_VERSION = "v2"

TAILORING_RULES = (
    ("You are tailoring, not ranking: fit_analysis was computed by Job Hunt OS's Ranker. Use its strong matches, "
     "resume gaps and recommended focus to choose emphasis; never rescore it or decide whether to apply."),
    NO_INVENTION_RULE,
    GAPS_STAY_GAPS_RULE,
    "Use only candidate_profile, active_resume and relevant_evidence from get_tailoring_context.",
    "Never change employers, job titles or employment dates; they are kept from the master resume automatically.",
    "Never state visa, sponsorship, citizenship or work-authorization facts beyond candidate_profile.work_authorization.",
    ("Every rewritten factual bullet cites the evidence it uses in evidence_ids. A bullet with no evidence_ids must "
     "restate a bullet of that role in the master resume."),
    "Only use numbers that appear in the cited evidence or the master resume.",
    ("The summary, cover_letter and screening_answer_drafts are checked the same way: every number, technology, "
     "employer and other name in them must already appear in the master resume, the candidate profile or the "
     "verified evidence. Naming this job's company and role is fine."),
    "skills_order only reorders skills that already appear in the master resume, profile skills or evidence.",
    ("When active_resume.roles is not empty, put bullets in experience_sections under the role_index they belong "
     "to (1-5 per role); otherwise use resume_bullets."),
    "The master resume is never modified: each save creates a new tailored version.",
    UNTRUSTED_DATA_RULE,
)

RESULT_SHAPE = """{"job_id": 123, "summary": "...", "skills_order": ["..."],
 "experience_sections": [{"role_index": 0, "bullets": [{"text": "...", "evidence_ids": [12]}]}],
 "resume_bullets": [], "cover_letter": "...", "screening_answer_drafts": {}}"""


def build_mcp_tailoring_prompt(minimum_fit_score: float | None = None, limit: int = 5) -> str:
    scope = (f"jobs waiting for tailoring with a fit score of at least {minimum_fit_score:g}"
             if minimum_fit_score is not None else "the jobs waiting for tailoring")
    return instructions(
        f"Tailor my resume for up to {limit} of {scope} in Job Hunt OS.",
        "Steps: 1) call list_jobs_for_tailoring; 2) call get_tailoring_context with the chosen job_ids; "
        "3) write one tailored result per job; 4) save them with save_bulk_tailored_resumes; 5) summarise what was "
        "saved and show any rejected item with its validation errors, then fix and resave only those.",
        "Rules:\n" + "\n".join(f"- {rule}" for rule in TAILORING_RULES),
        f"Result shape:\n{RESULT_SHAPE}",
    )


# Shown to Claude Desktop when it connects: what the server is for and the order to call its tools in.
MCP_SERVER_INSTRUCTIONS = instructions(
    "Job Hunt OS: the user's local job search workspace. Claude does the tailoring and review reasoning; "
    "Job Hunt OS supplies compact context, validates what you save and decides every workflow state.",
    "Tailoring: list_jobs_for_tailoring → get_tailoring_context → save_bulk_tailored_resumes. "
    "Review: list_jobs_for_review → get_review_context → save_bulk_reviews. "
    "get_bulk_operation_status shows what a batch saved. "
    "Applying: list_jobs_for_application → get_job_packet → search_application_knowledge per form question "
    "(request_human_answer for what it cannot answer) → record_application_result once per job.",
    "Batches are limited (see the error message when a batch is too large). A rejected item comes back with "
    "validation errors: fix only that item and save it again. Never invent facts to get past validation.",
)
