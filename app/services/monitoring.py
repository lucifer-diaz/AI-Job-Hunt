"""Aggregates over LLM request telemetry for the Monitoring tab, its API and the AI Optimization Advisor.

Every query filters by account explicitly (llm_request_logs is not tenancy-filtered, because a request may belong
to no account). The all-accounts view is only for the admin; the API decides that.

Aggregation happens in Python over a bounded, metadata-only row set, so it works the same on PostgreSQL and SQLite.
Prompt and response text is never read here.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LLM_ERROR_TYPES, LLM_STAGES, Application, Job, LLMRequestLog
from app.services import redaction
from app.services.llm_telemetry import CONTRIBUTORS, DEBUG_TEXT_LIMIT

RANGES = {"today": 0, "7d": 7, "30d": 30, "90d": 90}
MAX_ROWS = 50_000
CONTEXT_BUCKETS = (("<25%", 0, 25), ("25–50%", 25, 50), ("50–75%", 50, 75), ("75–90%", 75, 90), (">90%", 90, None))
HIGH_CONTEXT_PERCENT = 50
NEAR_LIMIT_PERCENT = 90
ROW_COLUMNS = (LLMRequestLog.id, LLMRequestLog.created_at, LLMRequestLog.user_id, LLMRequestLog.stage,
               LLMRequestLog.provider, LLMRequestLog.model, LLMRequestLog.prompt_version, LLMRequestLog.status,
               LLMRequestLog.input_tokens, LLMRequestLog.output_tokens, LLMRequestLog.total_tokens,
               LLMRequestLog.usage_source, LLMRequestLog.estimated_context_tokens, LLMRequestLog.context_window_size,
               LLMRequestLog.context_utilization_percent, LLMRequestLog.context_breakdown, LLMRequestLog.latency_ms,
               LLMRequestLog.estimated_cost, LLMRequestLog.cost_source, LLMRequestLog.retry_count,
               LLMRequestLog.error_type, LLMRequestLog.structured_output_valid, LLMRequestLog.workflow_type,
               LLMRequestLog.workflow_entity_id, LLMRequestLog.cached, LLMRequestLog.full_available_context_tokens,
               LLMRequestLog.selected_context_tokens, LLMRequestLog.context_reduction_percent,
               LLMRequestLog.raw_job_tokens, LLMRequestLog.selected_job_context_tokens,
               LLMRequestLog.job_context_reduction_percent, LLMRequestLog.requested_model, LLMRequestLog.worker_type,
               LLMRequestLog.model_mode, LLMRequestLog.configured_model, LLMRequestLog.model_group,
               LLMRequestLog.routing_tier, LLMRequestLog.routing_cause, LLMRequestLog.fallback_used,
               LLMRequestLog.escalated, LLMRequestLog.escalation_reason, LLMRequestLog.confidence,
               LLMRequestLog.config_version, LLMRequestLog.litellm_call_id, LLMRequestLog.schema_enforcement,
               LLMRequestLog.validation_failure_reason, LLMRequestLog.context_builder_version)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class MonitoringFilter:
    start: datetime
    end: datetime
    user_id: int | None  # None = every account (admin only)
    provider: str | None = None
    model: str | None = None
    stage: str | None = None
    prompt_version: str | None = None
    status: str | None = None
    workflow_type: str | None = None
    entity_id: int | None = None
    worker_type: str | None = None
    config_version: int | None = None
    routing_tier: str | None = None

    def public(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "all_accounts": self.user_id is None,
                "provider": self.provider, "model": self.model, "stage": self.stage,
                "prompt_version": self.prompt_version, "status": self.status, "workflow_type": self.workflow_type,
                "entity_id": self.entity_id, "worker_type": self.worker_type,
                "config_version": self.config_version, "routing_tier": self.routing_tier}


def time_range(name: str | None = None, start: datetime | None = None, end: datetime | None = None,
               now: datetime | None = None) -> tuple[datetime, datetime]:
    """`today`, `7d`, `30d` or `90d` ending now, or an explicit start and end."""
    now = now or utcnow()
    if start is not None:
        return start, end or now
    days = RANGES.get(name or "7d", 7)
    begin = now.replace(hour=0, minute=0, second=0, microsecond=0) if days == 0 else now - timedelta(days=days)
    return begin, end or now


def _money(value: Decimal | float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)])


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def off_configuration(r: Any) -> bool | None:
    """Whether the request was answered by a model other than the one configured for it. None when that is not
    known: no configured model recorded, the auto router (whose choice is the configuration), or no answer recorded.

    Compared at the gateway's own level: the configured name is a deployment (model group), so the answering model
    group is compared with it, and the provider's model name only when the gateway reported no group."""
    configured = r.configured_model
    actual = r.model_group or r.model
    if not configured or not actual or configured == "AUTO_ROUTER" or r.status == "FAILED":
        return None
    return actual != configured


def rows(db: Session, f: MonitoringFilter, columns: tuple = ROW_COLUMNS) -> list[Any]:
    query = select(*columns).where(LLMRequestLog.created_at >= f.start, LLMRequestLog.created_at <= f.end)
    if f.user_id is not None:
        query = query.where(LLMRequestLog.user_id == f.user_id)
    for column, value in ((LLMRequestLog.provider, f.provider), (LLMRequestLog.model, f.model),
                          (LLMRequestLog.stage, f.stage), (LLMRequestLog.prompt_version, f.prompt_version),
                          (LLMRequestLog.status, f.status), (LLMRequestLog.workflow_type, f.workflow_type),
                          (LLMRequestLog.workflow_entity_id, f.entity_id), (LLMRequestLog.worker_type, f.worker_type),
                          (LLMRequestLog.config_version, f.config_version), (LLMRequestLog.routing_tier, f.routing_tier)):
        if value not in (None, ""):
            query = query.where(column == value)
    return db.execute(query.order_by(LLMRequestLog.created_at.desc()).limit(MAX_ROWS)).all()


# ───────────── aggregates ─────────────

def summary(items: list[Any]) -> dict[str, Any]:
    requests = len(items)
    failed = sum(r.status == "FAILED" for r in items)
    latencies = [r.latency_ms for r in items if r.latency_ms is not None]
    costs = [r.estimated_cost for r in items if r.estimated_cost is not None]
    checked = [r for r in items if r.structured_output_valid is not None]
    contexts = [r.estimated_context_tokens for r in items if r.estimated_context_tokens is not None]
    utilization = [r.context_utilization_percent for r in items if r.context_utilization_percent is not None]
    return {
        "requests": requests, "succeeded": requests - failed, "failed": failed, "failure_rate": _rate(failed, requests),
        "input_tokens": sum(r.input_tokens or 0 for r in items), "output_tokens": sum(r.output_tokens or 0 for r in items),
        "total_tokens": sum(r.total_tokens or 0 for r in items),
        "estimated_token_requests": sum(r.usage_source == "estimated" for r in items),
        "requests_without_usage": sum(r.usage_source == "none" for r in items),
        # NULL means unknown, not free: with no priced request the total is unknown, and unpriced_requests says why.
        "estimated_cost": _money(sum(costs, Decimal(0))) if costs else None,
        "unpriced_requests": sum(r.estimated_cost is None for r in items),
        "avg_latency_ms": _mean(latencies), "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95), "p99_latency_ms": percentile(latencies, 99),
        "retried_requests": sum((r.retry_count or 0) > 0 for r in items),
        "retry_rate": _rate(sum((r.retry_count or 0) > 0 for r in items), requests),
        "structured_output_failures": sum(r.structured_output_valid is False for r in items),
        "structured_output_failure_rate": _rate(sum(r.structured_output_valid is False for r in items), len(checked)),
        # Why answers failed their worker's contract; POSSIBLE_CROSS_WORKER_OUTPUT is a worker doing another's job.
        "validation_failure_reasons": dict(Counter(r.validation_failure_reason for r in items
                                                   if r.validation_failure_reason)),
        "cross_worker_outputs": sum(r.validation_failure_reason == "POSSIBLE_CROSS_WORKER_OUTPUT" for r in items),
        "schema_enforcement": dict(Counter(r.schema_enforcement for r in items if r.schema_enforcement)),
        "avg_context_tokens": _mean(contexts), "p95_context_tokens": percentile(contexts, 95),
        "avg_context_utilization_percent": _mean(utilization),
        "cached_requests": sum(bool(r.cached) for r in items),
    }


def _contributors(items: list[Any]) -> dict[str, float]:
    totals = Counter()
    for r in items:
        for name, tokens in (r.context_breakdown or {}).items():
            totals[name] += tokens or 0
    whole = sum(totals.values())
    return {name: round(totals[name] / whole, 4) for name in CONTRIBUTORS if whole and totals[name]}


def by_stage(items: list[Any]) -> list[dict[str, Any]]:
    groups = defaultdict(list)
    for r in items:
        groups[r.stage].append(r)
    total_requests = len(items)
    total_cost = sum((r.estimated_cost for r in items if r.estimated_cost is not None), Decimal(0))
    out = []
    for stage in LLM_STAGES:
        group = groups.get(stage, [])
        stats = summary(group)
        cost = Decimal(str(stats["estimated_cost"])) if stats["estimated_cost"] is not None else None
        out.append({"stage": stage, **stats, "request_share": _rate(len(group), total_requests),
                    "cost_share": round(float(cost / total_cost), 4) if cost is not None and total_cost else None,
                    "context_contributors": _contributors(group),
                    "models": sorted({r.model or "" for r in group} - {""})})
    return out


def by_model(items: list[Any]) -> list[dict[str, Any]]:
    groups = defaultdict(list)
    for r in items:
        groups[(r.provider, r.model or "unknown")].append(r)
    out = []
    for (provider, model), group in groups.items():
        stats = summary(group)
        out.append({"provider": provider, "model": model, **stats,
                    "free": all(r.cost_source == "free" for r in group),
                    "stages": sorted({r.stage for r in group})})
    return sorted(out, key=lambda m: (-(m["estimated_cost"] or 0), -m["requests"]))


def timeseries(items: list[Any], f: MonitoringFilter) -> list[dict[str, Any]]:
    days: dict[date, dict[str, Any]] = {}
    day = f.start.date()
    while day <= f.end.date():
        days[day] = {"day": day.isoformat(), "requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0,
                     "estimated_cost": 0.0, "unpriced_requests": 0}
        day += timedelta(days=1)
    for r in items:
        bucket = days.get(r.created_at.date())
        if bucket is None:
            continue
        bucket["requests"] += 1
        bucket["failed"] += r.status == "FAILED"
        bucket["input_tokens"] += r.input_tokens or 0
        bucket["output_tokens"] += r.output_tokens or 0
        if r.estimated_cost is None:
            bucket["unpriced_requests"] += 1
        else:
            bucket["estimated_cost"] = round(bucket["estimated_cost"] + float(r.estimated_cost), 6)
    for bucket in days.values():  # a day with requests but none priced is unknown, not free; an idle day is $0
        if bucket["requests"] and bucket["unpriced_requests"] == bucket["requests"]:
            bucket["estimated_cost"] = None
    return list(days.values())


def latency(items: list[Any]) -> dict[str, Any]:
    stats = summary(items)
    return {"overall": {key: stats[key] for key in ("avg_latency_ms", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms")},
            "by_stage": [{"stage": s["stage"], "requests": s["requests"], "avg_latency_ms": s["avg_latency_ms"],
                          "p50_latency_ms": s["p50_latency_ms"], "p95_latency_ms": s["p95_latency_ms"],
                          "p99_latency_ms": s["p99_latency_ms"]} for s in by_stage(items) if s["requests"]]}


def context(items: list[Any]) -> dict[str, Any]:
    buckets = {label: 0 for label, _, _ in CONTEXT_BUCKETS}
    unknown = 0
    for r in items:
        pct = r.context_utilization_percent
        if pct is None:
            unknown += 1
            continue
        label = next(label for label, low, high in CONTEXT_BUCKETS if high is None or pct < high)
        buckets[label] += 1
    known = [r for r in items if r.context_utilization_percent is not None]
    near = sorted((r for r in known if r.context_utilization_percent >= HIGH_CONTEXT_PERCENT),
                  key=lambda r: -r.context_utilization_percent)[:20]
    return {"buckets": [{"label": label, "requests": n} for label, n in buckets.items()],
            "selection": context_selection(items),
            "unknown_window": unknown, "avg_utilization_percent": _mean([r.context_utilization_percent for r in known]),
            "avg_context_tokens": summary(items)["avg_context_tokens"],
            "high_context_requests": sum(r.context_utilization_percent >= HIGH_CONTEXT_PERCENT for r in known),
            "near_limit_requests": sum(r.context_utilization_percent >= NEAR_LIMIT_PERCENT for r in known),
            "contributors": _contributors(items),
            "largest": [{"id": r.id, "stage": r.stage, "model": r.model, "context_tokens": r.estimated_context_tokens,
                         "context_window_size": r.context_window_size,
                         "utilization_percent": r.context_utilization_percent} for r in near]}


def context_selection(items: list[Any]) -> dict[str, Any]:
    """How much dynamic context selection (app.services.context_builder) trimmed, overall and per stage."""
    measured = [r for r in items if r.selected_context_tokens is not None and r.full_available_context_tokens]

    def totals(group: list[Any]) -> dict[str, Any]:
        full = sum(r.full_available_context_tokens for r in group)
        sent = sum(r.selected_context_tokens for r in group)
        posting = [r for r in group if r.selected_job_context_tokens is not None and r.raw_job_tokens]
        raw_job = sum(r.raw_job_tokens for r in posting)
        sent_job = sum(r.selected_job_context_tokens for r in posting)
        return {"requests": len(group), "full_available_context_tokens": full, "selected_context_tokens": sent,
                "context_reduction_percent": round(100 * (full - sent) / full, 1) if full else None,
                "avg_reduction_percent": _mean([r.context_reduction_percent for r in group
                                                if r.context_reduction_percent is not None]),
                # The posting side: the whole job description against the requirements actually sent.
                "job_requests": len(posting), "raw_job_tokens": raw_job,
                "selected_job_context_tokens": sent_job,
                "job_context_reduction_percent": round(100 * (raw_job - sent_job) / raw_job, 1) if raw_job else None}

    stages = sorted({r.stage for r in measured})
    return {**totals(measured), "by_stage": [{"stage": stage, **totals([r for r in measured if r.stage == stage])}
                                             for stage in stages]}


def errors(items: list[Any]) -> dict[str, Any]:
    counts = Counter(r.error_type or "OTHER" for r in items if r.status == "FAILED")
    return {"failed": sum(counts.values()), "failure_rate": _rate(sum(counts.values()), len(items)),
            "by_type": [{"error_type": t, "requests": counts.get(t, 0)} for t in LLM_ERROR_TYPES],
            "by_stage": [{"stage": s["stage"], "failed": s["failed"], "failure_rate": s["failure_rate"]}
                         for s in by_stage(items) if s["failed"]]}


# ───────────── model routing (LiteLLM gateway mode) ─────────────

def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def routing(items: list[Any]) -> dict[str, Any]:
    """How requests were routed: per worker and model, the auto router's tiers, escalation and fallback.

    Answers: how often AUTO chooses each tier; how often a worker escalates and why; which model has the most
    schema failures; which is slowest; how much inference time each worker and model consumes (wall-clock latency
    is the measure available for local models). Only requests made in gateway mode carry a worker type.
    """
    routed = [r for r in items if r.worker_type]
    total_time = sum(r.latency_ms or 0 for r in routed)

    def block(group: list[Any]) -> dict[str, Any]:
        latencies = [r.latency_ms for r in group if r.latency_ms is not None]
        checked = [r for r in group if r.structured_output_valid is not None]
        first = [r for r in group if not r.escalated]
        confidences = [r.confidence for r in group if r.confidence is not None]
        compared = [x for x in (off_configuration(r) for r in group) if x is not None]
        return {"requests": len(group), "failure_rate": _rate(sum(r.status == "FAILED" for r in group), len(group)),
                "schema_failure_rate": _rate(sum(r.structured_output_valid is False for r in group), len(checked)),
                "escalations": sum(bool(r.escalated) for r in group),
                "escalation_rate": _rate(sum(bool(r.escalated) for r in group), len(first)),
                "fallbacks": sum(r.fallback_used is True for r in group),
                "fallback_rate": _rate(sum(r.fallback_used is True for r in group),
                                       sum(r.fallback_used is not None for r in group)),
                "failovers": sum(r.routing_cause == "FAILOVER" for r in group),
                "failover_rate": _rate(sum(r.routing_cause == "FAILOVER" for r in group), len(group)),
                "model_mismatch_rate": _rate(sum(compared), len(compared)), "model_mismatch_samples": len(compared),
                "avg_latency_ms": _mean(latencies), "p50_latency_ms": percentile(latencies, 50),
                "p95_latency_ms": percentile(latencies, 95), "p99_latency_ms": percentile(latencies, 99),
                "inference_ms": sum(latencies), "inference_share": _share(sum(latencies), total_time),
                "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
                "input_tokens": sum(r.input_tokens or 0 for r in group),
                "output_tokens": sum(r.output_tokens or 0 for r in group)}

    by_worker: dict[str, list[Any]] = defaultdict(list)
    by_model: dict[str, list[Any]] = defaultdict(list)
    by_pair: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in routed:
        by_worker[r.worker_type].append(r)
        by_model[r.model or r.requested_model or "unknown"].append(r)
        by_pair[(r.worker_type, r.model or r.requested_model or "unknown")].append(r)
    auto = [r for r in routed if r.model_mode == "AUTO_ROUTER" and r.configured_model == "AUTO_ROUTER"]
    tiers = Counter(r.routing_tier or "UNKNOWN" for r in auto)
    reasons = Counter((r.worker_type, r.escalation_reason) for r in routed if r.escalated)
    workers = sorted(({"worker_type": w, **block(g),
                       "configured": Counter(r.configured_model for r in g if r.configured_model).most_common(3),
                       "models": Counter(r.model for r in g if r.model).most_common(3)} for w, g in by_worker.items()),
                     key=lambda x: -x["requests"])
    models = sorted(({"model": m, **block(g)} for m, g in by_model.items()), key=lambda x: -x["requests"])
    return {
        "requests": len(routed), "inference_ms": total_time,
        "workers": workers, "models": models,
        "worker_models": [{"worker_type": w, "model": m, **block(g)} for (w, m), g in sorted(by_pair.items())],
        "auto_router": {"requests": len(auto), "tiers": [{"tier": t, "requests": n, "share": _share(n, len(auto))}
                                                         for t, n in tiers.most_common()]},
        "escalations": [{"worker_type": w, "reason": reason, "requests": n} for (w, reason), n in reasons.most_common()],
        "slowest_model": max(models, key=lambda m: m["p95_latency_ms"] or 0)["model"] if models else None,
        "most_schema_failures": max(models, key=lambda m: m["schema_failure_rate"] or 0)["model"]
        if any(m["schema_failure_rate"] for m in models) else None,
    }


def options(db: Session, f: MonitoringFilter) -> dict[str, list[str]]:
    """Values the filter selects can offer, from the whole range regardless of the other filters."""
    base = replace(f, provider=None, model=None, stage=None, prompt_version=None, status=None, workflow_type=None,
                   entity_id=None, worker_type=None,
                   config_version=None, routing_tier=None)
    items = rows(db, base)
    return {"providers": sorted({r.provider for r in items}), "models": sorted({r.model for r in items if r.model}),
            "stages": list(LLM_STAGES), "prompt_versions": sorted({r.prompt_version for r in items if r.prompt_version}),
            "statuses": ["SUCCEEDED", "FAILED"],
            "worker_types": sorted({r.worker_type for r in items if r.worker_type}),
            "config_versions": sorted({r.config_version for r in items if r.config_version is not None}, reverse=True),
            "routing_tiers": sorted({r.routing_tier for r in items if r.routing_tier})}


def dashboard(db: Session, f: MonitoringFilter) -> dict[str, Any]:
    items = rows(db, f)
    return {"filter": f.public(), "truncated": len(items) >= MAX_ROWS, "summary": summary(items),
            "stages": by_stage(items), "models": by_model(items), "timeseries": timeseries(items, f),
            "latency": latency(items), "context": context(items), "errors": errors(items), "routing": routing(items),
            "options": options(db, f)}


# ───────────── individual requests ─────────────

def _entity(db: Session, workflow_type: str | None, entity_id: int | None) -> dict[str, Any] | None:
    """A job or application label for a request, without any posting or resume text."""
    if entity_id is None:
        return None
    job = None
    if workflow_type == "job":
        job = db.get(Job, entity_id)
    elif workflow_type == "application":
        application = db.get(Application, entity_id)
        job = db.get(Job, application.job_id) if application else None
    if job is None:
        return {"type": workflow_type, "id": entity_id}
    return {"type": workflow_type, "id": entity_id, "job_id": job.id, "title": job.title, "company": job.company}


def request_row(r: Any) -> dict[str, Any]:
    return {"id": r.id, "created_at": r.created_at.isoformat(), "stage": r.stage, "provider": r.provider,
            "model": r.model, "prompt_version": r.prompt_version, "status": r.status, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "total_tokens": r.total_tokens, "usage_source": r.usage_source,
            "context_tokens": r.estimated_context_tokens, "context_window_size": r.context_window_size,
            "context_utilization_percent": r.context_utilization_percent, "latency_ms": r.latency_ms,
            "estimated_cost": _money(r.estimated_cost), "cost_source": r.cost_source, "retry_count": r.retry_count,
            "error_type": r.error_type, "structured_output_valid": r.structured_output_valid,
            "workflow_type": r.workflow_type, "workflow_entity_id": r.workflow_entity_id,
            "full_available_context_tokens": r.full_available_context_tokens,
            "selected_context_tokens": r.selected_context_tokens,
            "context_reduction_percent": r.context_reduction_percent,
            "raw_job_tokens": r.raw_job_tokens, "selected_job_context_tokens": r.selected_job_context_tokens,
            "job_context_reduction_percent": r.job_context_reduction_percent,
            # Model routing: the worker, what it was configured to use, what it asked for and what answered.
            "worker_type": r.worker_type, "model_mode": r.model_mode, "configured_model": r.configured_model,
            "requested_model": r.requested_model, "model_group": r.model_group, "routing_tier": r.routing_tier,
            "routing_cause": r.routing_cause, "fallback_used": r.fallback_used, "escalated": bool(r.escalated),
            "escalation_reason": r.escalation_reason, "confidence": r.confidence, "config_version": r.config_version,
            "litellm_call_id": r.litellm_call_id,
            # Structured output: how the contract was enforced and why an answer failed it.
            "schema_enforcement": r.schema_enforcement, "validation_failure_reason": r.validation_failure_reason,
            "context_builder_version": r.context_builder_version}


def list_requests(db: Session, f: MonitoringFilter, page: int = 1, per_page: int = 25) -> dict[str, Any]:
    items = rows(db, f)
    per_page = max(1, min(100, per_page))
    page = max(1, page)
    chunk = items[(page - 1) * per_page: page * per_page]
    return {"total": len(items), "page": page, "per_page": per_page, "items": [request_row(r) for r in chunk]}


def request_detail(db: Session, request_id: int, user_id: int | None) -> dict[str, Any] | None:
    """One request's metadata. Prompt text only appears when LLM_PROMPT_DEBUG stored it."""
    log = db.get(LLMRequestLog, request_id)
    if log is None or (user_id is not None and log.user_id != user_id):
        return None
    return {**request_row(log), "request_id": log.request_id, "external_request_id": log.external_request_id,
            "trace_id": log.trace_id, "prompt_name": log.prompt_name, "context_breakdown": log.context_breakdown,
            "cached": log.cached, "cache_read_tokens": log.cache_read_tokens, "cache_write_tokens": log.cache_write_tokens,
            "pricing_effective_from": log.pricing_effective_from.isoformat() if log.pricing_effective_from else None,
            "structured_output_retry_count": log.structured_output_retry_count, "error_message": log.error_message,
            "completed_at": log.completed_at.isoformat() if log.completed_at else None,
            "entity": _entity(db, log.workflow_type, log.workflow_entity_id),
            "trace_log_count": _trace_log_count(db, log.trace_id, user_id),
            # Redacted again on the way out: rows stored before debug text was redacted at write time are cleaned too.
            "debug": ({"prompt": redaction.redact_text(log.debug_prompt, DEBUG_TEXT_LIMIT),
                       "response": redaction.redact_text(log.debug_response, DEBUG_TEXT_LIMIT)}
                      if log.debug_prompt or log.debug_response else None)}


def _trace_log_count(db: Session, trace_id: str | None, user_id: int | None) -> int:
    """Monitoring → Logs lines written under the same trace as a request (its link to the logs)."""
    from sqlalchemy import func

    from app.models import SystemLogEntry

    if not trace_id:
        return 0
    query = select(func.count(SystemLogEntry.id)).where(SystemLogEntry.trace_id == trace_id)
    if user_id is not None:
        query = query.where(SystemLogEntry.user_id == user_id)
    return db.scalar(query) or 0


# ───────────── advisor input ─────────────

OUTLIERS = 5


def advisor_aggregates(db: Session, user_id: int, start: datetime, end: datetime) -> dict[str, Any]:
    """A compact, deterministic summary of one account's usage for the AI Optimization Advisor.

    No individual prompts, no text: counts, tokens, costs, rates and a handful of metadata-only outliers.
    """
    f = MonitoringFilter(start=start, end=end, user_id=user_id)
    items = rows(db, f)
    stages = [s for s in by_stage(items) if s["requests"]]
    succeeded = Counter((r.workflow_type, r.workflow_entity_id, r.stage, r.prompt_version) for r in items
                        if r.status == "SUCCEEDED" and r.workflow_entity_id is not None)
    duplicates = Counter()
    for (_, _, stage, _), n in succeeded.items():
        if n > 1:
            duplicates[stage] += n - 1

    def outlier(r: Any) -> dict[str, Any]:
        return {"id": r.id, "stage": r.stage, "model": r.model, "context_tokens": r.estimated_context_tokens,
                "utilization_percent": r.context_utilization_percent, "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens, "estimated_cost": _money(r.estimated_cost), "latency_ms": r.latency_ms}

    stage_fields = ("stage", "requests", "request_share", "failed", "failure_rate", "input_tokens", "output_tokens",
                    "estimated_cost", "cost_share", "unpriced_requests", "avg_latency_ms", "p95_latency_ms",
                    "retry_rate", "structured_output_failure_rate", "avg_context_tokens", "p95_context_tokens",
                    "avg_context_utilization_percent", "context_contributors", "models")
    model_fields = ("provider", "model", "free", "requests", "failure_rate", "structured_output_failure_rate",
                    "input_tokens", "output_tokens", "estimated_cost", "unpriced_requests", "avg_latency_ms",
                    "p95_latency_ms", "retry_rate", "stages")
    totals = summary(items)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": max(1, (end - start).days)},
        "totals": {k: totals[k] for k in ("requests", "failed", "failure_rate", "input_tokens", "output_tokens",
                                          "estimated_cost", "unpriced_requests", "avg_latency_ms", "p95_latency_ms",
                                          "retry_rate", "structured_output_failure_rate", "avg_context_tokens",
                                          "p95_context_tokens", "estimated_token_requests")},
        "stages": [{k: s[k] for k in stage_fields} for s in stages],
        "models": [{k: m[k] for k in model_fields} for m in by_model(items)],
        # The same model can behave differently per stage, so model comparisons use these per-stage rows.
        "stage_models": [{"stage": stage, **{k: m[k] for k in model_fields if k != "stages"}}
                         for stage in {s["stage"] for s in stages}
                         for m in by_model([r for r in items if r.stage == stage])],
        "prompt_versions": [{"stage": stage, "prompt_version": version, "requests": n} for (stage, version), n in
                            sorted(Counter((r.stage, r.prompt_version) for r in items).items(), key=lambda kv: -kv[1])],
        "errors": [e for e in errors(items)["by_type"] if e["requests"]],
        "context_buckets": context(items)["buckets"],
        "duplicate_successful_calls": [{"stage": stage, "extra_calls": n} for stage, n in duplicates.items()],
        # Gateway mode only: per-worker routing, escalation and inference-time aggregates (see routing()).
        "routing": _advisor_routing(routing(items)),
        "outliers": {
            "largest_context": [outlier(r) for r in sorted((r for r in items if r.estimated_context_tokens),
                                                           key=lambda r: -r.estimated_context_tokens)[:OUTLIERS]],
            "most_expensive": [outlier(r) for r in sorted((r for r in items if r.estimated_cost),
                                                          key=lambda r: -r.estimated_cost)[:OUTLIERS]],
        },
    }


def _advisor_routing(data: dict[str, Any]) -> dict[str, Any]:
    keep = ("requests", "failure_rate", "schema_failure_rate", "escalations", "escalation_rate", "fallback_rate",
            "avg_latency_ms", "p95_latency_ms", "inference_share", "avg_confidence")
    return {"requests": data["requests"],
            "workers": [{"worker_type": w["worker_type"], **{k: w[k] for k in keep},
                         "configured": [name for name, _ in w["configured"]], "models": [n for n, _ in w["models"]]}
                        for w in data["workers"]],
            "worker_models": [{"worker_type": x["worker_type"], "model": x["model"], **{k: x[k] for k in keep}}
                              for x in data["worker_models"]],
            "auto_router_tiers": data["auto_router"]["tiers"], "escalations": data["escalations"]}


def request_count(db: Session, user_id: int, start: datetime, end: datetime) -> int:
    return db.scalar(select(func.count(LLMRequestLog.id)).where(
        LLMRequestLog.user_id == user_id, LLMRequestLog.created_at >= start, LLMRequestLog.created_at <= end)) or 0


# ───────────── retention ─────────────

PRUNE_BATCH = 5000


def prune(db: Session, now: datetime | None = None, retention_days: int | None = None) -> dict[str, int]:
    """Roll raw request rows older than the retention into daily totals, then delete them. Commits.

    Daily totals (llm_usage_daily) are kept indefinitely; they hold counts, tokens, cost and latency only.
    """
    from sqlalchemy import delete

    from app.models import LLMUsageDaily
    from app.settings import get_settings

    from sqlalchemy import update

    debug_days = get_settings().llm_prompt_debug_retention_days
    cleared = 0
    if debug_days > 0:  # full prompt and response text goes first; the metadata row stays
        cleared = db.execute(update(LLMRequestLog)
                             .where(LLMRequestLog.created_at < (now or utcnow()) - timedelta(days=debug_days),
                                    (LLMRequestLog.debug_prompt.is_not(None)) | (LLMRequestLog.debug_response.is_not(None)))
                             .values(debug_prompt=None, debug_response=None)
                             .execution_options(synchronize_session=False)).rowcount or 0
        db.commit()
    days = retention_days if retention_days is not None else get_settings().llm_telemetry_retention_days
    if days <= 0:
        return {"rolled_up": 0, "deleted": 0, "debug_text_cleared": cleared}
    cutoff = (now or utcnow()) - timedelta(days=days)
    old = db.execute(select(*ROW_COLUMNS).where(LLMRequestLog.created_at < cutoff)
                     .order_by(LLMRequestLog.id).limit(PRUNE_BATCH)).all()
    if not old:
        return {"rolled_up": 0, "deleted": 0, "debug_text_cleared": cleared}
    groups: dict[tuple, list[Any]] = defaultdict(list)
    for r in old:
        groups[(r.user_id, r.created_at.date(), r.stage, r.provider, r.model or "", r.prompt_version or "")].append(r)
    for (user_id, day, stage, provider, model, version), items in groups.items():
        row = db.scalar(select(LLMUsageDaily).where(
            LLMUsageDaily.user_id.is_(None) if user_id is None else LLMUsageDaily.user_id == user_id,
            LLMUsageDaily.day == day, LLMUsageDaily.stage == stage, LLMUsageDaily.provider == provider,
            LLMUsageDaily.model == model, LLMUsageDaily.prompt_version == version))
        if row is None:
            row = LLMUsageDaily(user_id=user_id, day=day, stage=stage, provider=provider, model=model,
                                prompt_version=version, requests=0, failures=0, retries=0,
                                structured_output_failures=0, input_tokens=0, output_tokens=0, estimated_cost=None,
                                unpriced_requests=0, latency_ms_sum=0, latency_ms_max=0, context_tokens_sum=0)
            db.add(row)
        costs = [r.estimated_cost for r in items if r.estimated_cost is not None]
        row.requests += len(items)
        row.failures += sum(r.status == "FAILED" for r in items)
        row.retries += sum((r.retry_count or 0) > 0 for r in items)
        row.structured_output_failures += sum(r.structured_output_valid is False for r in items)
        row.input_tokens += sum(r.input_tokens or 0 for r in items)
        row.output_tokens += sum(r.output_tokens or 0 for r in items)
        if costs:
            row.estimated_cost = (row.estimated_cost or Decimal(0)) + sum(costs, Decimal(0))
        row.unpriced_requests += len(items) - len(costs)
        row.latency_ms_sum += sum(r.latency_ms or 0 for r in items)
        row.latency_ms_max = max([row.latency_ms_max, *[r.latency_ms or 0 for r in items]])
        row.context_tokens_sum += sum(r.estimated_context_tokens or 0 for r in items)
    deleted = db.execute(delete(LLMRequestLog).where(LLMRequestLog.id.in_([r.id for r in old]))
                         .execution_options(synchronize_session=False)).rowcount or 0
    db.commit()
    return {"rolled_up": len(groups), "deleted": deleted, "debug_text_cleared": cleared}
