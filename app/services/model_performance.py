"""Model performance, computed from raw LLM request telemetry (llm_request_logs) and nothing else.

Every figure here is an aggregate over rows app.services.llm_telemetry already wrote: there is no second telemetry
store, and no model or gateway is called to produce them. The browser reads these numbers from Job Hunt OS and never
talks to LiteLLM.

Six transparent dimensions per group, deliberately *not* folded into one "AI quality score":

    reliability | latency | throughput | cost | quality | context

Rules a change has to keep:
* NULL means unknown. A metric a provider did not report (time to first token, reasoning tokens, a price) is None and
  is left out of its own average; it is never zero and never estimated here. Counts say how many rows each rate used.
* `tokens_per_sec` is derived only from provider-reported output tokens and a measured or gateway-reported duration
  (time after the first token when TTFT is known, otherwise the model call's own duration). It is a per-request
  median, and it is None when neither duration exists.
* Cost is the sum of known request costs. Unpriced requests are counted, never priced; per-task figures say how many
  tasks they cover.
* Quality signals come from outcomes the system really stores (schema checks, reviewer decisions, corrections); a
  signal that is not recorded is listed in UNSUPPORTED rather than shown as 0.
* Aggregation happens in Python over a bounded row set, like app.services.monitoring, so PostgreSQL and SQLite agree.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ApplicationCommunicationEvent,
    JobFeedbackEvent,
    KnowledgeAnswer,
    LLMRequestLog,
    ResumeVariant,
)
from app.services.monitoring import off_configuration, percentile

L = LLMRequestLog
# Every column the aggregates read. Metadata and numbers only: never prompt or response text.
COLUMNS = (L.id, L.created_at, L.user_id, L.stage, L.provider, L.model, L.requested_model, L.prompt_version,
           L.status, L.error_type, L.retry_count, L.latency_ms, L.ttft_ms, L.generation_ms, L.queue_wait_ms,
           L.gateway_overhead_ms, L.input_tokens, L.output_tokens, L.total_tokens, L.cache_read_tokens,
           L.reasoning_tokens, L.usage_source, L.estimated_context_tokens, L.context_window_size,
           L.context_utilization_percent, L.full_available_context_tokens, L.selected_context_tokens,
           L.estimated_cost, L.cost_source, L.structured_output_valid, L.structured_output_retry_count,
           L.validation_failure_reason, L.workflow_type, L.workflow_entity_id, L.worker_type, L.configured_model,
           L.config_version, L.routing_tier, L.service_tier, L.fallback_used, L.escalated, L.escalation_reason,
           L.confidence, L.model_group, L.routing_cause, L.retrieval_ms)

GROUPINGS: dict[str, Callable[[Any], str]] = {
    "model": lambda r: r.model or r.requested_model or "unknown",
    "provider": lambda r: r.provider or "unknown",
    "worker": lambda r: r.worker_type or "unattributed",
    "stage": lambda r: r.stage or "OTHER",
    "prompt_version": lambda r: r.prompt_version or "unversioned",
    "config_version": lambda r: f"v{r.config_version}" if r.config_version is not None else "unrecorded",
    "routing_tier": lambda r: r.routing_tier or "unrecorded",
    "service_tier": lambda r: r.service_tier or "unrecorded",
    "hour": lambda r: r.created_at.strftime("%Y-%m-%d %H:00"),
    "day": lambda r: r.created_at.strftime("%Y-%m-%d"),
}
TIME_GROUPINGS = {"hour": 3600, "day": 86400}
LOW_SAMPLE = 20

# What cannot be reported yet, and why. Shown by the API and UI so a missing number is never mistaken for zero.
UNSUPPORTED = {
    "ttft": "Time to first token needs a streaming request. No adapter streams today, so it is recorded only if "
            "an adapter reports it (llm_request_logs.ttft_ms); otherwise it stays unknown.",
    "tool_calls": "Workers make single-shot structured requests; no tool calls go through the LLM layer, so tool-call "
                  "success is not recorded.",
    "reasoning_tokens": "Recorded only when the provider itemises them (OpenAI-shaped completion_tokens_details). "
                        "Anthropic and Gemini direct calls do not.",
    "generation_time": "Taken from LiteLLM's x-litellm-response-duration-ms and x-litellm-overhead-duration-ms "
                       "headers; direct-provider requests do not report it.",
    "queue_wait": "Measured as the wait for a worker's max-concurrency slot; with no limit set there is no queue "
                  "and nothing is recorded. Provider-side queueing is not observable.",
    "service_tier": "Recorded when the response names one (OpenAI-style service_tier).",
    "per_model_quality": "Reviewer scores, inbox corrections and user feedback are stored per worker/outcome, not "
                         "per model, so quality outcomes are not split by model.",
}


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _avg(values: list[float], digits: int = 2) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def decode_rate(r: Any) -> float | None:
    """Output tokens per second for one request, or None when it cannot be derived reliably."""
    if r.status == "FAILED" or r.usage_source != "provider_reported" or not r.output_tokens:
        return None
    if r.ttft_ms is not None and r.latency_ms is not None and r.latency_ms > r.ttft_ms:
        ms = r.latency_ms - r.ttft_ms
    elif r.generation_ms:
        ms = r.generation_ms
    else:
        return None
    return r.output_tokens / (ms / 1000)


def task_key(r: Any) -> tuple | None:
    """One unit of work: the same worker stage on the same job/application (an escalation or retry is the same task)."""
    if r.workflow_entity_id is None:
        return None
    return (r.user_id, r.workflow_type, r.workflow_entity_id, r.stage)


def peak_concurrency(items: list[Any]) -> int | None:
    """The most requests in flight at once, from each row's start and latency. None without any latency."""
    events: list[tuple[datetime, int]] = []
    for r in items:
        if r.latency_ms is None:
            continue
        events.append((r.created_at, 1))
        events.append((r.created_at + timedelta(milliseconds=r.latency_ms), -1))
    if not events:
        return None
    events.sort(key=lambda e: (e[0], e[1]))  # an end before a start at the same instant
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def window_seconds(start: datetime, end: datetime, now: datetime | None = None) -> float:
    """The length of the range the rates are per, never more than has elapsed and never under a minute."""
    now = now or datetime.now(UTC).replace(tzinfo=None)  # telemetry timestamps are naive UTC
    return max(60.0, (min(end, now) - start).total_seconds())


def block(items: list[Any], seconds: float) -> dict[str, Any]:
    """All six dimensions for one set of requests. `seconds` is the window the per-minute rates divide by."""
    total = len(items)
    ok = [r for r in items if r.status != "FAILED"]
    failed = total - len(ok)
    minutes = seconds / 60
    latencies = [r.latency_ms for r in items if r.latency_ms is not None]
    ok_latencies = [r.latency_ms for r in ok if r.latency_ms is not None]
    ttfts = [r.ttft_ms for r in items if r.ttft_ms is not None]
    generations = [r.generation_ms for r in items if r.generation_ms is not None]
    waits = [r.queue_wait_ms for r in items if r.queue_wait_ms is not None]
    rates = [x for x in (decode_rate(r) for r in items) if x is not None]
    errors = Counter(r.error_type or "OTHER" for r in items if r.status == "FAILED")
    checked = [r for r in items if r.structured_output_valid is not None]
    routed = [r for r in items if r.fallback_used is not None]
    first_try = [r for r in items if not r.escalated]
    clean = [r for r in items if r.status != "FAILED" and not (r.retry_count or 0)
             and not (r.structured_output_retry_count or 0) and not r.escalated]
    with_tokens = [r for r in ok if r.output_tokens and r.latency_ms is not None]
    inputs = [r.input_tokens for r in items if r.input_tokens is not None]
    outputs = [r.output_tokens for r in items if r.output_tokens is not None]
    paired = [r for r in items if r.input_tokens and r.output_tokens is not None]
    cached = [r.cache_read_tokens for r in items if r.cache_read_tokens is not None]
    reasoning = [r.reasoning_tokens for r in items if r.reasoning_tokens is not None]
    utilization = [r.context_utilization_percent for r in items if r.context_utilization_percent is not None]
    contexts = [r.estimated_context_tokens for r in items if r.estimated_context_tokens is not None]
    selection = [r for r in items if r.selected_context_tokens is not None and r.full_available_context_tokens]
    confidences = [r.confidence for r in items if r.confidence is not None]
    compared = [x for x in (off_configuration(r) for r in items) if x is not None]
    retrievals = [r.retrieval_ms for r in items if getattr(r, "retrieval_ms", None) is not None]
    per_minute = Counter(r.created_at.replace(second=0, microsecond=0) for r in items)

    priced = [r for r in items if r.estimated_cost is not None]
    cost_total = float(sum(float(r.estimated_cost) for r in priced)) if priced else None
    tasks: dict[tuple, bool] = {}
    task_cost = 0.0
    task_priced = 0
    for r in items:
        key = task_key(r)
        if key is None:
            continue
        tasks[key] = tasks.get(key, False) or r.status != "FAILED"
        if r.estimated_cost is not None:
            task_cost += float(r.estimated_cost)
            task_priced += 1
    task_rows = sum(task_key(r) is not None for r in items)
    done = sum(tasks.values())

    return {
        "requests": total, "low_sample": total < LOW_SAMPLE,
        "reliability": {
            "succeeded": len(ok), "failed": failed, "success_rate": _rate(len(ok), total),
            "failure_rate": _rate(failed, total), "error_types": dict(errors),
            "timeout_rate": _rate(errors.get("TIMEOUT", 0), total),
            "rate_limit_rate": _rate(errors.get("RATE_LIMIT", 0), total),
            "retried_requests": sum((r.retry_count or 0) > 0 for r in items),
            "retry_rate": _rate(sum((r.retry_count or 0) > 0 for r in items), total),
            # Succeeded first time: no retry, no schema correction, no escalation. 1 − this is the retry cost.
            "retry_adjusted_success_rate": _rate(len(clean), total),
            "fallback_requests": sum(r.fallback_used is True for r in routed),
            "fallback_rate": _rate(sum(r.fallback_used is True for r in routed), len(routed)),
            "fallback_unknown": total - len(routed),
            # Failover (app.services.failover) moved the request off a resting model before it was sent; the
            # gateway's own fallback is fallback_rate above. Configuration mismatch: answered by another model than
            # configured, over the requests where both are known (model_mismatch_samples).
            "failover_requests": sum(r.routing_cause == "FAILOVER" for r in items),
            "failover_rate": _rate(sum(r.routing_cause == "FAILOVER" for r in items), total),
            "model_mismatch_requests": sum(compared), "model_mismatch_samples": len(compared),
            "model_mismatch_rate": _rate(sum(compared), len(compared)),
            "escalated_requests": sum(bool(r.escalated) for r in items),
            "escalation_rate": _rate(sum(bool(r.escalated) for r in items), len(first_try)),
            "schema_checked": len(checked),
            "schema_pass_rate": _rate(sum(r.structured_output_valid is True for r in checked), len(checked)),
            "schema_failure_rate": _rate(sum(r.structured_output_valid is False for r in checked), len(checked)),
        },
        "latency": {
            "avg_ms": _avg(latencies, 0), "p50_ms": percentile(latencies, 50), "p75_ms": percentile(latencies, 75),
            "p95_ms": percentile(latencies, 95), "p99_ms": percentile(latencies, 99),
            "successful_p50_ms": percentile(ok_latencies, 50), "successful_p95_ms": percentile(ok_latencies, 95),
            "ttft_p50_ms": percentile(ttfts, 50), "ttft_p95_ms": percentile(ttfts, 95), "ttft_samples": len(ttfts),
            "generation_avg_ms": _avg(generations, 0), "generation_samples": len(generations),
            "queue_wait_p95_ms": percentile(waits, 95), "queue_wait_samples": len(waits),
            # Evidence retrieval before the call (llm_request_logs.retrieval_ms); unknown when none ran.
            "retrieval_p50_ms": percentile(retrievals, 50), "retrieval_p95_ms": percentile(retrievals, 95),
            "retrieval_samples": len(retrievals),
            "tokens_per_sec": _rounded(percentile(rates, 50), 1), "tokens_per_sec_samples": len(rates),
            # Wall-clock milliseconds per generated token, over successful requests that reported both.
            "ms_per_output_token": _rounded(sum(r.latency_ms for r in with_tokens)
                                            / sum(r.output_tokens for r in with_tokens), 2) if with_tokens else None,
        },
        "throughput": {
            "window_minutes": round(minutes, 1),
            "requests_per_min": _rounded(total / minutes, 3), "requests_per_hour": _rounded(total * 60 / minutes, 2),
            "successful_requests_per_min": _rounded(len(ok) / minutes, 3),
            "input_tokens_per_min": _rounded(sum(inputs) / minutes, 1) if inputs else None,
            "output_tokens_per_min": _rounded(sum(outputs) / minutes, 1) if outputs else None,
            "peak_requests_per_min": max(per_minute.values()) if per_minute else None,
            "peak_concurrency": peak_concurrency(items),
        },
        "tokens": {
            "input": sum(inputs), "output": sum(outputs), "total": sum(r.total_tokens or 0 for r in items),
            "cached_input": sum(cached) if cached else None, "reasoning": sum(reasoning) if reasoning else None,
            "requests_without_usage": total - len(inputs),
            "estimated_usage_requests": sum(r.usage_source == "estimated" for r in items),
            "output_input_ratio": _rounded(sum(r.output_tokens for r in paired) / sum(r.input_tokens for r in paired),
                                           3) if paired else None,
            "cache_hit_share": _rounded(sum(cached) / sum(inputs), 4) if cached and sum(inputs) else None,
        },
        "context": {
            "avg_utilization_percent": _avg(utilization), "p95_utilization_percent": percentile(utilization, 95),
            "avg_context_tokens": _avg(contexts, 0), "window_unknown": total - len(utilization),
            "selected_tokens": sum(r.selected_context_tokens for r in selection),
            "available_tokens": sum(r.full_available_context_tokens for r in selection),
            "selection_reduction_percent": _rounded(100 * (1 - sum(r.selected_context_tokens for r in selection)
                                                           / sum(r.full_available_context_tokens for r in selection)),
                                                    1) if selection else None,
        },
        "cost": {
            # NULL cost means unknown, not free. These cover priced requests only and say how many were not.
            "total": _rounded(cost_total, 6), "priced_requests": len(priced), "unpriced_requests": total - len(priced),
            "free": bool(items) and all(r.cost_source == "free" for r in items),
            "per_request": _rounded(cost_total / len(priced), 6) if priced else None,
            "per_successful_request": _rounded(cost_total / sum(r.status != "FAILED" for r in priced), 6)
            if priced and any(r.status != "FAILED" for r in priced) else None,
            "tasks": len(tasks), "successful_tasks": done, "task_requests": task_rows,
            "unpriced_task_requests": task_rows - task_priced,
            # What it cost, failed attempts included, to get one task done / to process 1,000 tasks.
            "per_successful_task": _rounded(task_cost / done, 6) if done and task_priced else None,
            "per_1k_tasks": _rounded(1000 * task_cost / len(tasks), 4) if tasks and task_priced else None,
        },
        "quality": {
            "schema_pass_rate": _rate(sum(r.structured_output_valid is True for r in checked), len(checked)),
            "schema_checked": len(checked),
            "validation_failure_reasons": dict(Counter(r.validation_failure_reason for r in items
                                                       if r.validation_failure_reason)),
            "retry_after_invalid_output_rate": _rate(sum((r.structured_output_retry_count or 0) > 0 for r in items),
                                                     total),
            "avg_confidence": _avg(confidences, 3), "confidence_samples": len(confidences),
        },
    }


def compare(items: list[Any], group_by: str, start: datetime, end: datetime,
            now: datetime | None = None) -> dict[str, Any]:
    """The same block for every value of `group_by`, plus each group's share of the whole.

    Time groupings (hour, day) are rated per bucket length so a series is comparable across buckets; the others share
    the range's window, so requests/min is comparable across models."""
    if group_by not in GROUPINGS:
        raise ValueError(f"unknown grouping {group_by!r}")
    key = GROUPINGS[group_by]
    seconds = TIME_GROUPINGS.get(group_by) or window_seconds(start, end, now)
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in items:
        groups[key(r)].append(r)
    whole = block(items, window_seconds(start, end, now))
    total = len(items)
    all_fallbacks = whole["reliability"]["fallback_requests"]
    all_escalations = whole["reliability"]["escalated_requests"]
    all_cost = whole["cost"]["total"]
    out = []
    for label, group in groups.items():
        b = block(group, seconds)
        rel, cost = b["reliability"], b["cost"]
        out.append({"key": label, "label": label, **b, "share": {
            "requests": _rate(len(group), total),
            "fallbacks": _rate(rel["fallback_requests"], all_fallbacks),
            "escalations": _rate(rel["escalated_requests"], all_escalations),
            "cost": _rate(cost["total"], all_cost) if cost["total"] is not None and all_cost else None}})
    out.sort(key=(lambda g: g["key"]) if group_by in TIME_GROUPINGS else (lambda g: (-g["requests"], g["key"])))
    return {"group_by": group_by, "groups": out, "totals": whole, "unsupported": UNSUPPORTED}


def compact(items: list[Any], seconds: float, limit: int = 3) -> list[dict[str, Any]]:
    """The few numbers an AI node shows per model that actually answered: volume, success, p95, throughput,
    schema pass rate and cost. `cost` is None when no request was priced (unknown, not free)."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in items:
        groups[GROUPINGS["model"](r)].append(r)
    out = []
    for model, group in groups.items():
        b = block(group, seconds)
        out.append({"model": model, "known": model != "unknown", "requests": b["requests"],
                    "share": _rate(len(group), len(items)), "success_rate": b["reliability"]["success_rate"],
                    "p95_latency_ms": b["latency"]["p95_ms"], "requests_per_min": b["throughput"]["requests_per_min"],
                    "tokens_per_sec": b["latency"]["tokens_per_sec"],
                    "schema_pass_rate": b["reliability"]["schema_pass_rate"], "cost": b["cost"]["total"],
                    "unpriced_requests": b["cost"]["unpriced_requests"], "free": b["cost"]["free"],
                    "low_sample": b["low_sample"]})
    out.sort(key=lambda m: (-m["requests"], m["model"]))
    return out[:limit]


# ───────────── outcomes people and workers stored (quality, where a real signal exists) ─────────────

def quality_signals(db: Session, user_id: int, start: datetime, end: datetime) -> dict[str, Any]:
    """Outcome signals the product already stores, per worker. Not a model quality score, and not split by model.

    Each entry has `available`; the ones that cannot be computed are named in `unavailable` with the reason."""
    variants = db.execute(select(ResumeVariant.reviewer_score, ResumeVariant.reviewer_notes).where(
        ResumeVariant.user_id == user_id, ResumeVariant.created_at >= start, ResumeVariant.created_at <= end,
        ResumeVariant.reviewer_score.is_not(None)).limit(5000)).all()
    decisions = Counter((v[1] or {}).get("decision") or "UNRECORDED" for v in variants)
    scores = [float(v[0]) for v in variants]
    ev = ApplicationCommunicationEvent
    events = db.execute(select(ev.user_corrected).where(
        ev.user_id == user_id, ev.created_at >= start, ev.created_at <= end).limit(5000)).all()
    feedback = dict(db.execute(select(JobFeedbackEvent.action, func.count(JobFeedbackEvent.id)).where(
        JobFeedbackEvent.user_id == user_id, JobFeedbackEvent.created_at >= start,
        JobFeedbackEvent.created_at <= end).group_by(JobFeedbackEvent.action)).all())
    asked = db.execute(select(KnowledgeAnswer.answer).where(
        KnowledgeAnswer.user_id == user_id, KnowledgeAnswer.request_context.is_not(None),
        KnowledgeAnswer.created_at >= start, KnowledgeAnswer.created_at <= end).limit(5000)).all()
    return {
        "reviewer": {"worker": "REVIEWER", "available": bool(variants), "reviewed": len(variants),
                     "decisions": dict(decisions), "median_score": percentile(scores, 50),
                     "pass_rate": _rate(decisions.get("PASS", 0), len(variants)),
                     "retailor_rate": _rate(decisions.get("RETAILOR", 0), len(variants))},
        "inbox_corrections": {"worker": "INBOX", "available": bool(events), "events": len(events),
                              "corrected": sum(bool(e[0]) for e in events),
                              "correction_rate": _rate(sum(bool(e[0]) for e in events), len(events))},
        "user_feedback": {"worker": "RANKER", "available": bool(feedback), "actions": feedback},
        "human_answers": {"worker": "APPLICATION", "available": bool(asked), "asked": len(asked),
                          "answered": sum(bool((a[0] or "").strip()) for a in asked)},
        "unavailable": {
            "answer_resolver_correction_rate": "Answers a person edits after the resolver filled them are not tracked, "
                                               "so only questions handed to a person are counted (human_answers).",
            "task_completion_rate": "Per-stage task completion is on AI Orchestration and Running workers.",
            "acceptance_by_model": UNSUPPORTED["per_model_quality"],
        },
    }
