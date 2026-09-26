"""LLM request telemetry, captured once in the shared LLM layer (app.services.llm) for every provider.

    business service → LLMClient.generate_prompt → start telemetry → model request → capture usage + latency
    → validate output → persist LLMRequestLog → return the response

Rules:
* Recording never changes the outcome of the request: every failure here is logged and swallowed.
* Provider-reported usage is preferred; otherwise tokens are estimated locally and marked `estimated`.
  Metrics that cannot be reported or estimated stay NULL.
* Context size is always estimated from the prompt text, split by the labelled sections app.prompts builds.
* No prompt or response text is stored unless LLM_PROMPT_DEBUG is on, and then only after central redaction
  (app.services.redaction), like error messages.
* Structured output is recorded per request: how the contract was enforced (`schema_enforcement`), whether the answer
  passed it, and why not (`validation_failure_reason`: INVALID_JSON, WRONG_OUTPUT_SCHEMA,
  POSSIBLE_CROSS_WORKER_OUTPUT or SEMANTIC_VALIDATION).
"""
from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.services import model_catalog, redaction, tracing
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger("jobhunt.llm_telemetry")

# Prompt name (app.prompts Prompt.name) → workflow stage shown on the Monitoring tab.
STAGE_BY_PROMPT = {
    "ranking": "RANKER", "tailoring": "TAILOR", "cover_letter": "TAILOR", "review": "REVIEWER", "resume_parser": "RESUME_PARSER",
    "evidence_extraction": "RESUME_PARSER", "application_question": "APPLICATION", "followup": "FOLLOW_UP",
    "inbox": "INBOX", "company_insights": "COMPANY_INSIGHTS", "job_description_parser": "OTHER", "ai_optimization": "ADVISOR",
}
VALIDATION_FAILURE_REASONS = ("INVALID_JSON", "WRONG_OUTPUT_SCHEMA", "POSSIBLE_CROSS_WORKER_OUTPUT",
                              "SEMANTIC_VALIDATION")
CONTRIBUTORS = ("system_prompt", "candidate_profile", "resume", "evidence", "job_description", "conversation",
                "research", "other")
# Section labels written by app.prompts.common.section/untrusted → the context contributor they belong to.
SECTION_CONTRIBUTOR = {
    "CANDIDATE PROFILE": "candidate_profile", "CANDIDATE POLICY": "candidate_profile",
    "ACTIVE RESUME": "resume", "RESUME": "resume", "MASTER RESUME": "resume", "MASTER RESUME ROLES": "resume",
    "RESUME TEXT": "resume", "TAILORED RESUME": "resume", "RELEVANT EXPERIENCE": "resume", "RELEVANT SKILLS": "resume",
    "VERIFIED RESUME FACTS": "resume", "UNVERIFIED RESUME FACTS": "resume", "REJECTED RESUME FACTS": "resume",
    "VERIFIED EVIDENCE": "evidence", "KNOWN ANSWERS": "candidate_profile", "CANDIDATE": "candidate_profile",
    "SOURCE RESUME": "resume", "RELEVANT EVIDENCE": "evidence", "TAILORED CLAIMS": "resume",
    "FIT ANALYSIS (completed by the Ranker; read-only)": "job_description", "QUESTION": "conversation",
    "ANSWER OPTIONS": "conversation", "SCHEMA CORRECTION": "other",
    "JOB ID": "job_description", "JOB": "job_description", "JOB POSTING": "job_description",
    "JOB REQUIREMENTS": "job_description", "POSTING SECTIONS": "job_description",
    "JOB DESCRIPTION": "job_description", "FIT ANALYSIS": "job_description",
    "PREVIOUS FOLLOW-UPS": "conversation", "EMAIL SENDER": "conversation", "EMAIL SUBJECT": "conversation",
    "EMAIL BODY": "conversation", "APPLICATION": "conversation", "KNOWN APPLICATIONS": "conversation",
    "RECRUITERS (the first is the primary contact)": "conversation",
    "COMPANY RESEARCH": "research",
}
_SECTION_LINE = re.compile(
    r"^(?P<label>" + "|".join(re.escape(label) for label in sorted(SECTION_CONTRIBUTOR, key=len, reverse=True))
    + r")(?: \(untrusted data\))?:(?: |$)", re.M)
MAX_ERROR_MESSAGE = redaction.MAX_ERROR_MESSAGE
DEBUG_TEXT_LIMIT = 200_000


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def estimate_tokens(text: str | None) -> int:
    """A deliberately simple local estimate (about 4 characters per token), used only when providers report nothing."""
    return math.ceil(len(text) / 4) if text else 0


def redact(message: str | None) -> str | None:
    """Secrets out of an error message. One implementation for every kind of telemetry: app.services.redaction."""
    return redaction.redact_text(message, MAX_ERROR_MESSAGE)


def context_breakdown(system: str, user: str) -> dict[str, int]:
    """Estimated tokens per context contributor, from the labelled sections of the prompt."""
    tokens = dict.fromkeys(CONTRIBUTORS, 0)
    tokens["system_prompt"] = estimate_tokens(system)
    matches = list(_SECTION_LINE.finditer(user or ""))
    tokens["other"] += estimate_tokens((user or "")[:matches[0].start()] if matches else user)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(user)
        tokens[SECTION_CONTRIBUTOR[match.group("label")]] += estimate_tokens(user[match.start():end])
    return tokens


@dataclass
class LLMCall:
    """What a business service tells the LLM layer about a request. No monitoring code lives in services."""

    workflow_type: str | None = None  # job | application | profile | system | agent
    entity_id: int | None = None
    attempt: int = 1  # the caller's 1-based attempt number; retry_count = attempt - 1
    # Checks the parsed output; its return value is handed back as LLMResult.validated. Raising marks the
    # request as a structured-output failure and the exception reaches the caller unchanged.
    validate: Callable[[dict[str, Any]], Any] | None = None
    structured_output_retry_count: int = 0
    # Set by services that build task-specific context (app.services.context_builder.ContextStats.as_dict()).
    context_stats: dict[str, Any] | None = None
    # LiteLLM gateway mode (app.services.model_resolution). The worker is normally read from the prompt's name;
    # `worker_type` names it explicitly (configuration tests), `resolved` runs an unapplied configuration (worker
    # tests), and `escalate` asks for the worker's escalation model when its policy allows MANUAL escalation.
    worker_type: str | None = None
    resolved: Any = None
    escalate: bool = False
    # Set by the LLM layer: the prompt's output contract, schema corrections used, and whether escalation is off
    # (a correction retry is never escalated again).
    contract: Any = None
    schema_corrections: int = 0
    no_escalation: bool = False


@dataclass
class Usage:
    """Token usage for one request. `input_tokens` includes cached and cache-write tokens."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    uncached_input_tokens: int | None = None  # what the base input rate applies to
    reported_cost: float | None = None
    source: str = "provider_reported"


@dataclass
class Attempt:
    """Mutable facts collected while one request runs."""

    provider: str
    requested_model: str | None
    user_id: int | None
    call: LLMCall
    prompt_name: str | None
    prompt_version: str | None
    system: str
    user: str
    started_at: datetime = field(default_factory=utcnow)
    model: str | None = None
    usage: Usage | None = None
    response_text: str | None = None
    sent: bool = False  # False when the request was refused before reaching the provider
    structured_output_valid: bool | None = None
    # The generic external-call row for this same request (app.services.external_api), set by the LLM layer.
    external_request_id: str | None = None
    # Model routing, filled in gateway mode (app.services.model_resolution): see LLMRequestLog.
    worker_type: str | None = None
    model_mode: str | None = None
    configured_model: str | None = None
    config_version: int | None = None
    litellm_call_id: str | None = None
    model_group: str | None = None
    routing_tier: str | None = None
    routing_cause: str | None = None
    fallback_used: bool | None = None
    escalated: bool = False
    escalation_reason: str | None = None
    confidence: float | None = None
    context_window: int | None = None  # known from the model registry when the catalog has no entry
    schema_enforcement: str | None = None  # native_schema | json_mode | prompt_only | none
    validation_failure_reason: str | None = None


_sessions: Callable[[], Session] | None = None


def use_sessions(factory: Callable[[], Session] | None) -> None:
    """Point telemetry at another database (tests); None restores the application database."""
    global _sessions
    _sessions = factory


def _session_factory() -> Callable[[], Session]:
    if _sessions is not None:
        return _sessions
    from app.db import SessionLocal

    return SessionLocal


def build_entry(attempt: Attempt, *, latency_ms: int, error: BaseException | None, error_type: str | None,
                completed_at: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    usage = attempt.usage
    if usage is None and attempt.sent and error is None:
        usage = Usage(input_tokens=estimate_tokens(attempt.system) + estimate_tokens(attempt.user),
                      output_tokens=estimate_tokens(attempt.response_text), source="estimated")
        usage.uncached_input_tokens = usage.input_tokens
    breakdown = context_breakdown(attempt.system, attempt.user)
    context_tokens = usage.input_tokens if usage and usage.input_tokens is not None else sum(breakdown.values())
    model = attempt.model or attempt.requested_model
    window = model_catalog.context_window(attempt.provider, attempt.requested_model, attempt.started_at) \
        or model_catalog.context_window(attempt.provider, model, attempt.started_at) or attempt.context_window
    if not attempt.sent:
        cost = model_catalog.CostEstimate(Decimal(0), "not_sent")
    else:
        # Providers often answer with a dated snapshot of the requested model; price it as the requested model
        # when the snapshot itself has no catalog entry.
        priced = model if model_catalog.lookup(attempt.provider, model, attempt.started_at) else attempt.requested_model
        cost = model_catalog.estimate_cost(
            attempt.provider, priced or model, at=attempt.started_at,
            input_tokens=(usage.uncached_input_tokens if usage and usage.uncached_input_tokens is not None
                          else usage.input_tokens if usage else None),
            output_tokens=usage.output_tokens if usage else None,
            cache_read_tokens=usage.cache_read_tokens if usage else None,
            cache_write_tokens=usage.cache_write_tokens if usage else None,
            provider_reported_cost=usage.reported_cost if usage else None)
    total = (usage.input_tokens + usage.output_tokens
             if usage and usage.input_tokens is not None and usage.output_tokens is not None else None)
    call = attempt.call
    debug = settings.llm_prompt_debug
    return {
        "request_id": str(uuid.uuid4()), "user_id": attempt.user_id,
        "external_request_id": attempt.external_request_id, "trace_id": tracing.current_trace_id(),
        "workflow_type": call.workflow_type, "workflow_entity_id": call.entity_id,
        "stage": STAGE_BY_PROMPT.get(attempt.prompt_name or "", "OTHER"),
        "provider": attempt.provider[:30], "requested_model": (attempt.requested_model or "")[:300] or None,
        "model": (model or "")[:300] or None, "prompt_name": attempt.prompt_name, "prompt_version": attempt.prompt_version,
        "input_tokens": usage.input_tokens if usage else None, "output_tokens": usage.output_tokens if usage else None,
        "total_tokens": total, "usage_source": usage.source if usage else "none",
        "estimated_context_tokens": context_tokens, "context_window_size": window,
        "context_utilization_percent": round(100 * context_tokens / window, 2) if window else None,
        "context_breakdown": breakdown, "latency_ms": latency_ms,
        "estimated_cost": cost.cost, "cost_source": cost.source, "pricing_effective_from": cost.effective_from,
        "status": "FAILED" if error is not None else "SUCCEEDED", "retry_count": max(0, call.attempt - 1),
        "error_type": error_type, "error_message": redact(f"{type(error).__name__}: {error}") if error else None,
        "structured_output_valid": attempt.structured_output_valid,
        "structured_output_retry_count": call.structured_output_retry_count,
        "cached": bool(usage and usage.cache_read_tokens),
        "cache_read_tokens": usage.cache_read_tokens if usage else None,
        "cache_write_tokens": usage.cache_write_tokens if usage else None,
        **_context_selection(call.context_stats),
        **_routing(attempt),
        "schema_enforcement": attempt.schema_enforcement,
        "validation_failure_reason": attempt.validation_failure_reason,
        "context_builder_version": ((call.context_stats or {}).get("context_builder_version") or None),
        "debug_prompt": redaction.redact_text(f"{attempt.system}\n\n{attempt.user}", DEBUG_TEXT_LIMIT) if debug else None,
        "debug_response": redaction.redact_text(attempt.response_text, DEBUG_TEXT_LIMIT)
        if debug and attempt.response_text else None,
        "created_at": attempt.started_at, "completed_at": completed_at or utcnow(),
    }


def _context_selection(stats: dict[str, Any] | None) -> dict[str, Any]:
    stats = stats or {}
    return {key: stats.get(key) for key in ("full_available_context_tokens", "selected_context_tokens",
                                            "context_reduction_percent", "raw_job_tokens",
                                            "selected_job_context_tokens", "job_context_reduction_percent")}


def _routing(attempt: Attempt) -> dict[str, Any]:
    return {"worker_type": attempt.worker_type, "model_mode": attempt.model_mode,
            "configured_model": (attempt.configured_model or "")[:200] or None,
            "config_version": attempt.config_version, "litellm_call_id": (attempt.litellm_call_id or "")[:64] or None,
            "model_group": (attempt.model_group or "")[:200] or None, "routing_tier": attempt.routing_tier,
            "routing_cause": (attempt.routing_cause or "")[:60] or None, "fallback_used": attempt.fallback_used,
            "escalated": attempt.escalated, "escalation_reason": attempt.escalation_reason,
            "confidence": attempt.confidence}


STRUCTURED_OUTPUT_ERRORS = ("SCHEMA_VALIDATION", "INVALID_JSON")


def _structured_output_retries(db: Session, entry: dict[str, Any]) -> int:
    """Earlier structured-output failures for the same entity and stage since its last success."""
    from sqlalchemy import func, select

    from app.models import LLMRequestLog

    if entry["workflow_entity_id"] is None:
        return entry["structured_output_retry_count"]
    same = (LLMRequestLog.user_id == entry["user_id"], LLMRequestLog.stage == entry["stage"],
            LLMRequestLog.workflow_type == entry["workflow_type"],
            LLMRequestLog.workflow_entity_id == entry["workflow_entity_id"])
    last_success = db.scalar(select(func.max(LLMRequestLog.created_at)).where(*same, LLMRequestLog.status == "SUCCEEDED"))
    query = select(func.count(LLMRequestLog.id)).where(*same, LLMRequestLog.error_type.in_(STRUCTURED_OUTPUT_ERRORS))
    if last_success is not None:
        query = query.where(LLMRequestLog.created_at > last_success)
    return max(entry["structured_output_retry_count"], db.scalar(query) or 0)


def persist(entry: dict[str, Any]) -> None:
    from app.models import LLMRequestLog

    with _session_factory()() as db:
        entry = {**entry, "structured_output_retry_count": _structured_output_retries(db, entry)}
        db.add(LLMRequestLog(**entry))
        db.commit()


def record(attempt: Attempt, *, latency_ms: int, error: BaseException | None = None,
           error_type: str | None = None) -> None:
    """Store one request's telemetry. Never raises: monitoring must not break the request it observes."""
    try:
        if not get_settings().llm_telemetry_enabled:
            return
        persist(build_entry(attempt, latency_ms=latency_ms, error=error, error_type=error_type))
    except Exception:  # noqa: BLE001  telemetry is best effort by design
        log.warning("could not record LLM telemetry for %s/%s", attempt.provider, attempt.requested_model,
                    exc_info=True)
