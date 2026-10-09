"""The AI workers Job Hunt OS runs, what each needs from a model, and its default model assignment.

A worker is identified by the prompt it sends (app.prompts Prompt.name), so the LLM layer can resolve a worker's
model centrally and no business service ever names a model. Defaults here are *configuration seeds*: they are
copied into an account's WorkerModelConfig rows once (app.services.ai_model_config.ensure_seeded) and are edited
there from then on. Nothing at request time reads DEFAULT_ASSIGNMENTS.
"""
from __future__ import annotations

from dataclasses import dataclass

FAST, PRIMARY_REASONING, DEEP_REVIEW = "FAST", "PRIMARY_REASONING", "DEEP_REVIEW"
AUTO = "AUTO"  # the tier that names the LiteLLM auto router, for workers that want to point at it by tier

# Default logical tiers. The models behind them are configuration: blank until mapped (or seeded from
# LITELLM_TIER_FAST / _PRIMARY / _DEEP / LITELLM_AUTO_ROUTER_MODEL).
DEFAULT_TIERS = (
    (FAST, "Small, quick model for high-volume classification and extraction", "litellm_tier_fast"),
    (PRIMARY_REASONING, "The main reasoning model for parsing, ranking and tailoring", "litellm_tier_primary"),
    (DEEP_REVIEW, "A larger (ideally different-family) model for independent review", "litellm_tier_deep"),
    (AUTO, "The LiteLLM auto router", "litellm_auto_router_model"),
)


@dataclass(frozen=True)
class Worker:
    worker_type: str
    label: str
    prompts: tuple[str, ...]  # app.prompts names this worker sends; empty = reserved for a future prompt
    # Machine-facing workers parse the answer as typed JSON: a model that cannot return JSON reliably is unusable.
    structured_output_required: bool
    # Smallest context a model should give this worker (tokens). A smaller known window is a warning.
    min_context_tokens: int
    default_mode: str  # FIXED | TIER | AUTO_ROUTER
    default_tier: str  # the tier used in TIER mode, and by AUTO_ROUTER workers while the auto router is off
    legacy_stage: str | None = None  # the pre-existing stage model (Settings / RANK_MODEL …) this worker inherits
    description: str = ""


WORKERS: dict[str, Worker] = {w.worker_type: w for w in (
    Worker("RESUME_PARSER", "Resume Parser", ("resume_parser",), True, 8192, "TIER", PRIMARY_REASONING,
           description="Parses an uploaded resume into structured facts"),
    Worker("JD_PARSER", "JD Parser", ("job_description_parser",), True, 8192, "TIER", PRIMARY_REASONING,
           description="Parses a job posting into structured requirements"),
    # Fallback only: picks the description out of blocks of a page that deterministic extraction could not settle.
    Worker("JD_RECOVERY", "JD Recovery", ("job_description_recovery",), True, 8192, "TIER", FAST,
           description="Identifies which blocks of a fetched posting page are the job description (never writes it)"),
    Worker("RANKER", "Ranker", ("ranking",), True, 8192, "TIER", PRIMARY_REASONING, "rank",
           description="Scores how well a job fits the candidate"),
    Worker("RESUME_MATCHER", "Resume Matcher", (), True, 8192, "TIER", PRIMARY_REASONING,
           description="Matches resume evidence to job requirements"),
    # The Tailor answers with a typed patch (app.prompts.contracts.TailoringResult) that is validated claim by claim.
    Worker("TAILOR", "Tailor", ("tailoring", "cover_letter"), True, 8192, "TIER", PRIMARY_REASONING, "tailor",
           description="Presents verified experience for a job (a patch of the resume) and writes the cover letter"),
    Worker("REVIEWER", "Reviewer", ("review",), True, 16384, "TIER", DEEP_REVIEW, "review",
           description="Independently reviews a tailored resume"),
    Worker("APPLICATION_ANSWER", "Application Answer", ("application_question",), True, 4096, "TIER",
           PRIMARY_REASONING, description="Drafts answers to application form questions"),
    # The Application Inbox (app.services.application_inbox): extracts what a matched recruiting email says, and
    # helps decide which application an email belongs to when deterministic matching is unsure. Never moves a job.
    Worker("INBOX_CLASSIFIER", "Inbox Classifier", ("inbox", "application_inbox", "application_email_match"), True,
           4096, "AUTO_ROUTER", FAST, description="Classifies recruiter and application email"),
    Worker("FOLLOWUP", "Follow-Up", ("followup",), False, 4096, "AUTO_ROUTER", FAST, "followup",
           description="Drafts follow-up messages"),
    # Job Insights (app.services.job_insights): synthesis and classification over research already fetched, so a
    # balanced model is enough; neither worker browses, and neither ever produces a salary.
    Worker("COMPANY_INSIGHTS", "Company Insights", ("company_insights",), True, 16384, "TIER", PRIMARY_REASONING,
           description="Organises fetched company research: official values, employee-reported themes, reported "
                       "interview stages and questions, each citing its sources"),
    Worker("INTERVIEW_INSIGHTS", "Interview Insights", ("interview_insights",), True, 16384, "TIER",
           PRIMARY_REASONING, description="Writes interview preparation from the posting, the fit analysis and "
                                          "verified evidence (labelled as generated)"),
    Worker("AI_OPTIMIZATION", "AI Optimization", ("ai_optimization",), True, 16384, "TIER", DEEP_REVIEW,
           description="The AI Optimization Advisor's analysis (advice only)"),
    Worker("JOB_NORMALIZATION", "Job Normalization", (), True, 4096, "AUTO_ROUTER", FAST,
           description="Normalises scraped job fields"),
    Worker("EVIDENCE_EXTRACTION", "Evidence Extraction", ("evidence_extraction",), True, 8192, "TIER",
           PRIMARY_REASONING, "tailor", description="Extracts verifiable claims from the master resume"),
)}
WORKER_BY_PROMPT = {prompt: w.worker_type for w in WORKERS.values() for prompt in w.prompts}
# The Monitoring stage (llm_request_logs.stage) each worker's requests have always been recorded under.
MONITORING_STAGE = {"RESUME_PARSER": "RESUME_PARSER", "EVIDENCE_EXTRACTION": "RESUME_PARSER", "RANKER": "RANKER",
                    "JD_PARSER": "JD_PARSER", "JD_RECOVERY": "JD_RECOVERY",
                    "TAILOR": "TAILOR", "REVIEWER": "REVIEWER", "APPLICATION_ANSWER": "APPLICATION",
                    "FOLLOWUP": "FOLLOW_UP", "INBOX_CLASSIFIER": "INBOX", "COMPANY_INSIGHTS": "COMPANY_INSIGHTS",
                    "INTERVIEW_INSIGHTS": "INTERVIEW_INSIGHTS",
                    "AI_OPTIMIZATION": "ADVISOR"}

# Quality-escalation conditions a worker policy may use (app.services.model_resolution.escalation_reason).
ESCALATION_CONDITIONS = ("LOW_CONFIDENCE", "SCHEMA_FAILURE", "AMBIGUOUS", "FATAL_REQUIREMENT_UNCERTAIN", "MANUAL")
AUTO_ROUTER_TIERS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")


def worker_for_prompt(prompt_name: str | None) -> str | None:
    return WORKER_BY_PROMPT.get(prompt_name or "")


def label(worker_type: str) -> str:
    worker = WORKERS.get(worker_type)
    return worker.label if worker else worker_type.replace("_", " ").title()
