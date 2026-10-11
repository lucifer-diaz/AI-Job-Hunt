"""Aggregates over external API telemetry: the External APIs, Apify and Logs sections of Monitoring.

Everything here reads rows the instrumentation layer wrote (app.services.external_api) and answers with plain
numbers — no text, no headers, no payloads. Aggregation happens in Python over a bounded, metadata-only row
set, exactly as the LLM side does (app.services.monitoring), so PostgreSQL and SQLite behave the same.

Costs are not guessed here. A row carries a cost only when the provider reported one; Apify's run cost is read
from the run itself (search_runs.usage_total_usd), which is the only place Apify states it, and is reported
separately from per-request costs so nothing is counted twice.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ExternalAPIRequestLog as Row
from app.models import Job, SavedSearch, SearchRun, SystemLogEntry
from app.services import backoff
from app.services.monitoring import MAX_ROWS, _mean, _money, _rate, percentile, time_range

ROW_COLUMNS = (Row.id, Row.request_id, Row.trace_id, Row.created_at, Row.user_id, Row.provider, Row.service,
               Row.operation, Row.workflow, Row.workflow_type, Row.workflow_entity_id, Row.job_id,
               Row.application_id, Row.http_method, Row.endpoint_host, Row.endpoint_path, Row.status,
               Row.http_status_code, Row.latency_ms, Row.request_size_bytes, Row.response_size_bytes,
               Row.attempt, Row.retry_count, Row.rate_limit_remaining, Row.error_type, Row.estimated_cost,
               Row.cost_currency, Row.cost_source, Row.completed_at)
TIMEOUTS = ("TIMEOUT",)
LOG_PAGE_MAX = 200


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ExternalFilter:
    start: datetime
    end: datetime
    user_id: int | None  # None = every account (admin only)
    provider: str | None = None
    service: str | None = None
    operation: str | None = None
    workflow: str | None = None
    status: str | None = None
    error_only: bool = False
    error_type: str | None = None      # TIMEOUT, RATE_LIMIT, CLIENT_ERROR… what the Status column shows
    http_status_code: int | None = None
    job_id: int | None = None
    application_id: int | None = None
    trace_id: str | None = None

    def public(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "all_accounts": self.user_id is None,
                "provider": self.provider, "service": self.service, "operation": self.operation,
                "workflow": self.workflow, "status": self.status, "error_only": self.error_only,
                "error_type": self.error_type, "http_status_code": self.http_status_code,
                "job_id": self.job_id, "application_id": self.application_id, "trace_id": self.trace_id}


def conditions(f: ExternalFilter) -> list[Any]:
    """Everything the filter matches on. One definition, so the list and its total never disagree."""
    where = [Row.created_at >= f.start, Row.created_at <= f.end]
    if f.user_id is not None:
        where.append(Row.user_id == f.user_id)
    for column, value in ((Row.provider, f.provider), (Row.service, f.service), (Row.operation, f.operation),
                          (Row.workflow, f.workflow), (Row.status, f.status), (Row.error_type, f.error_type),
                          (Row.http_status_code, f.http_status_code), (Row.job_id, f.job_id),
                          (Row.application_id, f.application_id), (Row.trace_id, f.trace_id)):
        if value not in (None, ""):
            where.append(column == value)
    if f.error_only:
        where.append(Row.status == "FAILED")
    return where


def rows(db: Session, f: ExternalFilter, limit: int = MAX_ROWS) -> list[Any]:
    return db.execute(select(*ROW_COLUMNS).where(*conditions(f))
                      .order_by(Row.created_at.desc(), Row.id.desc()).limit(limit)).all()


# ───────────── aggregates ─────────────

def _latencies(items: list[Any]) -> list[float]:
    return [r.latency_ms for r in items if r.latency_ms is not None]


def _cost(items: list[Any]) -> Decimal | None:
    costs = [r.estimated_cost for r in items if r.estimated_cost is not None]
    return sum(costs, Decimal(0)) if costs else None


def health(items: list[Any]) -> dict[str, Any]:
    """The reliability picture one provider (or everything) shows: how often, how badly and how slowly it fails."""
    total = len(items)
    failed = sum(r.status == "FAILED" for r in items)
    latencies = _latencies(items)
    errors = Counter(r.error_type for r in items if r.error_type)
    # A 5xx answer and an SDK error classified SERVER_ERROR are the same failure; count each row once.
    server_errors = sum(1 for r in items if r.error_type == "SERVER_ERROR"
                        or (r.http_status_code is not None and r.http_status_code >= 500))
    rate_limited = sum(1 for r in items if r.error_type == "RATE_LIMIT" or r.http_status_code == 429)
    timeouts = sum(errors.get(kind, 0) for kind in TIMEOUTS)
    return {
        "requests": total, "failed": failed,
        "success_rate": _rate(total - failed, total), "failure_rate": _rate(failed, total),
        "rate_limited": rate_limited, "rate_limit_rate": _rate(rate_limited, total),
        "server_errors": server_errors, "server_error_rate": _rate(server_errors, total),
        "client_errors": errors.get("CLIENT_ERROR", 0),
        "timeouts": timeouts, "timeout_rate": _rate(timeouts, total),
        "connection_errors": errors.get("CONNECTION", 0) + errors.get("DNS", 0),
        "retries": sum(r.retry_count or 0 for r in items),
        "retried_requests": sum((r.retry_count or 0) > 0 for r in items),
        "retry_rate": _rate(sum((r.retry_count or 0) > 0 for r in items), total),
        "avg_latency_ms": _mean(latencies), "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95), "p99_latency_ms": percentile(latencies, 99),
        "max_latency_ms": max(latencies) if latencies else None,
        "estimated_cost": _money(_cost(items)),
        "unpriced_requests": sum(r.estimated_cost is None for r in items),
        "request_bytes": sum(r.request_size_bytes or 0 for r in items),
        "response_bytes": sum(r.response_size_bytes or 0 for r in items),
    }


def summary(items: list[Any]) -> dict[str, Any]:
    return {**health(items), "providers": len({r.provider for r in items}),
            "operations": len({(r.provider, r.operation) for r in items})}


def by_provider(items: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in items:
        groups[r.provider].append(r)
    return sorted(({"provider": provider, **health(group)} for provider, group in groups.items()),
                  key=lambda row: -row["requests"])


def by_operation(items: list[Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for r in items:
        groups[(r.provider, r.service or "", r.operation or "")].append(r)
    return sorted(({"provider": p, "service": s, "operation": o, **health(group)}
                   for (p, s, o), group in groups.items()), key=lambda row: -row["requests"])


def by_workflow(items: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in items:
        groups[r.workflow or "—"].append(r)
    return sorted(({"workflow": workflow, **health(group)} for workflow, group in groups.items()),
                  key=lambda row: -row["requests"])


def errors(items: list[Any]) -> dict[str, Any]:
    failures = [r for r in items if r.status == "FAILED"]
    by_type = Counter(r.error_type or "OTHER" for r in failures)
    by_code = Counter(r.http_status_code for r in failures if r.http_status_code)
    return {
        "failed": len(failures), "failure_rate": _rate(len(failures), len(items)),
        "by_type": [{"error_type": kind, "requests": n, "share": _rate(n, len(failures))}
                    for kind, n in by_type.most_common()],
        "by_status_code": [{"http_status_code": code, "requests": n} for code, n in sorted(by_code.items())],
        "by_provider": [{"provider": provider, "failed": n} for provider, n in
                        Counter(r.provider for r in failures).most_common()],
    }


def timeseries(items: list[Any], f: ExternalFilter) -> list[dict[str, Any]]:
    """Per-day calls, failures, latency and cost — per hour when the range is a single day."""
    hourly = (f.end - f.start) <= timedelta(days=1)
    buckets: dict[datetime, list[Any]] = defaultdict(list)
    for r in items:
        moment = r.created_at.replace(minute=0, second=0, microsecond=0) if hourly \
            else r.created_at.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets[moment].append(r)
    return [{"at": moment.isoformat(), "requests": len(group),
             "failed": sum(r.status == "FAILED" for r in group),
             "avg_latency_ms": _mean(_latencies(group)), "estimated_cost": _money(_cost(group))}
            for moment, group in sorted(buckets.items())]


def latency(items: list[Any]) -> dict[str, Any]:
    """Average, p50, p95 and p99 overall and per provider, operation and workflow."""
    return {"overall": {k: v for k, v in health(items).items() if "latency" in k},
            "by_provider": [{"provider": row["provider"], **{k: row[k] for k in
                             ("requests", "avg_latency_ms", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms")}}
                            for row in by_provider(items)],
            "by_operation": [{k: row[k] for k in ("provider", "service", "operation", "requests", "avg_latency_ms",
                                                  "p50_latency_ms", "p95_latency_ms", "p99_latency_ms")}
                             for row in by_operation(items)[:20]],
            "by_workflow": [{k: row[k] for k in ("workflow", "requests", "avg_latency_ms", "p95_latency_ms")}
                            for row in by_workflow(items)]}


def row_card(r: Any) -> dict[str, Any]:
    return {"id": r.id, "request_id": r.request_id, "trace_id": r.trace_id, "created_at": r.created_at.isoformat(),
            "provider": r.provider, "service": r.service, "operation": r.operation, "workflow": r.workflow,
            "workflow_type": r.workflow_type, "workflow_entity_id": r.workflow_entity_id, "job_id": r.job_id,
            "application_id": r.application_id, "http_method": r.http_method, "endpoint_host": r.endpoint_host,
            "endpoint_path": r.endpoint_path, "status": r.status, "http_status_code": r.http_status_code,
            "latency_ms": r.latency_ms, "attempt": r.attempt, "retry_count": r.retry_count,
            "error_type": r.error_type, "estimated_cost": _money(r.estimated_cost),
            "cost_currency": r.cost_currency, "cost_source": r.cost_source,
            "request_size_bytes": r.request_size_bytes, "response_size_bytes": r.response_size_bytes}


def captured(row: Any) -> dict[str, Any] | None:
    """The captured request and response of one call, redacted again on read; None when nothing was captured."""
    from app.services.redaction import redact_headers, redact_text
    from app.settings import get_settings

    payload = getattr(row, "payload", None)
    if not isinstance(payload, dict):
        return None
    limit = max(get_settings().external_api_payload_limit, get_settings().external_api_capture_limit)

    def part(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {**{k: v for k, v in value.items() if k in ("method", "status")},
                "url": redact_text(value.get("url"), 2000) if value.get("url") else None,
                "headers": redact_headers(value.get("headers") or {}),
                "body": redact_text(value.get("body"), limit) if value.get("body") else None}
    return {"request": part(payload.get("request")), "response": part(payload.get("response")),
            "error": redact_text(payload.get("error"), 2000) if payload.get("error") else None}


def list_requests(db: Session, f: ExternalFilter, page: int = 1, per_page: int = 25) -> dict[str, Any]:
    total = db.scalar(select(func.count(Row.id)).where(*conditions(f))) or 0
    per_page = max(1, min(per_page, 100))
    items = rows(db, f, limit=per_page * page)[(page - 1) * per_page: page * per_page]
    return {"items": [row_card(r) for r in items], "total": total, "page": page, "per_page": per_page,
            "pages": max(1, -(-total // per_page))}


def request_detail(db: Session, request_row_id: int, user_id: int | None) -> dict[str, Any] | None:
    """One call, with its safe metadata and the LLM request it belongs to when there is one."""
    from app.models import LLMRequestLog

    row = db.get(Row, request_row_id)
    if row is None or (user_id is not None and row.user_id != user_id):
        return None
    llm = db.scalar(select(LLMRequestLog).where(LLMRequestLog.external_request_id == row.request_id))
    entity = None
    if row.job_id or (row.workflow_type == "job" and row.workflow_entity_id):
        job = db.get(Job, row.job_id or row.workflow_entity_id)
        entity = {"type": "job", "id": job.id, "label": f"{job.title} · {job.company}"} if job else None
    return {
        **row_card(row),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "rate_limit_remaining": row.rate_limit_remaining,
        "rate_limit_reset_at": row.rate_limit_reset_at.isoformat() if row.rate_limit_reset_at else None,
        "error_message": row.error_message, "metadata": row.request_metadata, "entity": entity,
        "payload": captured(row),
        "llm": ({"id": llm.id, "stage": llm.stage, "model": llm.model, "input_tokens": llm.input_tokens,
                 "output_tokens": llm.output_tokens, "estimated_cost": _money(llm.estimated_cost),
                 "structured_output_valid": llm.structured_output_valid, "prompt_version": llm.prompt_version}
                if llm else None),
    }


# ───────────── Apify ─────────────

def _run_import(run: SearchRun) -> dict[str, Any]:
    stats = (run.stats or {}).get("import") if isinstance(run.stats, dict) else None
    return stats if isinstance(stats, dict) else {}


def apify_runs(db: Session, f: ExternalFilter, limit: int = 200) -> list[SearchRun]:
    query = select(SearchRun).where(SearchRun.provider == "apify", SearchRun.created_at >= f.start,
                                    SearchRun.created_at <= f.end)
    if f.user_id is not None:
        query = query.where(SearchRun.user_id == f.user_id)
    return list(db.scalars(query.order_by(SearchRun.id.desc()).limit(limit)).all())


def run_location(run: SearchRun) -> str:
    """Where a run searched, as one label. The actor's input holds whatever that actor takes: a string for most, a
    list for multi-location actors (["Bengaluru", "Singapore"]), occasionally an object. Grouping needs a string."""
    value = (run.input_json or {}).get("location") if isinstance(run.input_json, dict) else None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v).strip() for v in value if v not in (None, "") and str(v).strip())
    elif isinstance(value, dict):
        value = ", ".join(str(v).strip() for v in value.values() if isinstance(v, (str, int, float)) and str(v).strip())
    elif value is not None and not isinstance(value, str):
        value = str(value)
    return (value or "").strip() or run.location or ""


def run_card(run: SearchRun, setup_names: dict[int, str]) -> dict[str, Any]:
    stats = _run_import(run)
    return {"id": run.id, "external_run_id": run.external_run_id, "actor_id": run.actor_id, "status": run.status,
            "created_at": run.created_at.isoformat(),
            "finished_at": run.completed_at.isoformat() if run.completed_at else None,
            "duration_ms": (int((run.completed_at - run.created_at).total_seconds() * 1000)
                            if run.completed_at else None),
            "location": run_location(run),
            "saved_search_id": run.saved_search_id, "saved_search": setup_names.get(run.saved_search_id or -1),
            "search_profile_id": run.search_profile_id,
            "dataset_id": run.dataset_id or None, "dataset_item_count": run.dataset_item_count,
            "jobs_discovered": run.item_count, "jobs_normalized": stats.get("normalized"),
            "jobs_deduplicated": stats.get("duplicates"), "jobs_created": run.created_jobs,
            "compute_units": run.compute_units, "memory_mb": run.memory_mb,
            "reported_cost": _money(run.usage_total_usd), "error": run.error or None}


def apify_summary(db: Session, f: ExternalFilter, items: list[Any] | None = None) -> dict[str, Any]:
    """What Apify did in this range: runs, what they found, and what Apify itself said they used and cost.

    Compute units and cost are only ever Apify's own figures. A run Apify has not reported usage for stays
    null rather than being estimated.
    """
    runs = apify_runs(db, f)
    calls = [r for r in (items if items is not None else rows(db, f)) if r.provider == "APIFY"]
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    setups = {s.id: s.name for s in db.scalars(select(SavedSearch)).all()}
    finished = [r for r in runs if r.completed_at is not None]
    compute = [r.compute_units for r in runs if r.compute_units is not None]
    costs = [r.usage_total_usd for r in runs if r.usage_total_usd is not None]
    imports = [_run_import(r) for r in runs]

    def group(key, label):
        buckets: dict[Any, list[SearchRun]] = defaultdict(list)
        for run in runs:
            buckets[key(run)].append(run)
        return [{label: str(name or "—"), "runs": len(group),
                 "succeeded": sum(r.status == "SUCCEEDED" for r in group),
                 "failed": sum(r.status not in ("SUCCEEDED", "READY", "RUNNING") for r in group),
                 "jobs_discovered": sum(r.item_count or 0 for r in group),
                 "jobs_created": sum(r.created_jobs or 0 for r in group),
                 "compute_units": round(sum(r.compute_units for r in group if r.compute_units is not None), 4)
                                  if any(r.compute_units is not None for r in group) else None,
                 "reported_cost": _money(sum((r.usage_total_usd for r in group if r.usage_total_usd is not None),
                                             Decimal(0))) if any(r.usage_total_usd is not None for r in group) else None}
                for name, group in sorted(buckets.items(), key=lambda kv: -len(kv[1]))]

    return {
        "runs_today": sum(r.created_at >= today for r in runs),
        "runs_this_month": sum(r.created_at >= month for r in runs),
        "runs": len(runs),
        "succeeded": sum(r.status == "SUCCEEDED" for r in runs),
        "failed": sum(r.status in ("FAILED", "ABORTED", "TIMED-OUT") for r in runs),
        "running": sum(r.completed_at is None for r in runs),
        "success_rate": _rate(sum(r.status == "SUCCEEDED" for r in finished), len(finished)),
        "compute_units": round(sum(compute), 4) if compute else None,
        "runs_with_reported_usage": len(compute),
        "reported_cost": _money(sum(costs, Decimal(0))) if costs else None,
        "runs_with_reported_cost": len(costs),
        "dataset_items": sum(r.item_count or 0 for r in runs),
        "jobs_discovered": sum(r.item_count or 0 for r in runs),
        "jobs_normalized": sum(s.get("normalized", 0) for s in imports) or None,
        "jobs_deduplicated": sum(s.get("duplicates", 0) for s in imports) or None,
        "jobs_after_deduplication": sum(r.created_jobs or 0 for r in runs),
        "api": health(calls),
        "by_actor": group(lambda r: r.actor_id, "actor_id"),
        "by_saved_search": group(lambda r: setups.get(r.saved_search_id or -1), "saved_search"),
        "by_location": group(run_location, "location"),
        "by_site": _by_site(db, f),
        "by_day": sorted(group(lambda r: r.created_at.date().isoformat(), "day"), key=lambda row: row["day"]),
        "recent_runs": [run_card(r, setups) for r in runs[:25]],
    }


def _by_site(db: Session, f: ExternalFilter) -> list[dict[str, Any]]:
    """Jobs discovered per board, from the jobs an Apify run created (jobs.source is `apify:<site>`)."""
    query = select(Job.source, func.count(Job.id)).where(Job.discovered_at >= f.start, Job.discovered_at <= f.end,
                                                         Job.source.like("apify:%"))
    if f.user_id is not None:
        query = query.where(Job.user_id == f.user_id)
    return [{"site": source.split(":", 1)[1] or "—", "jobs": count}
            for source, count in sorted(db.execute(query.group_by(Job.source)).all(), key=lambda kv: -kv[1])]


# ───────────── logs ─────────────

def logs(db: Session, *, start: datetime, end: datetime, user_id: int | None, category: str | None = None,
         level: str | None = None, provider: str | None = None, workflow: str | None = None,
         trace_id: str | None = None, search: str | None = None, page: int = 1,
         per_page: int = 50) -> dict[str, Any]:
    """The Monitoring → Logs table: newest first, filtered and paginated. History is never returned unbounded."""
    conditions = [SystemLogEntry.created_at >= start, SystemLogEntry.created_at <= end]
    if user_id is not None:
        conditions.append(SystemLogEntry.user_id == user_id)
    for column, value in ((SystemLogEntry.category, category), (SystemLogEntry.level, level),
                          (SystemLogEntry.provider, provider), (SystemLogEntry.workflow, workflow),
                          (SystemLogEntry.trace_id, trace_id)):
        if value not in (None, ""):
            conditions.append(column == value)
    if search:
        conditions.append(SystemLogEntry.message.ilike(f"%{search[:120]}%"))
    per_page = max(1, min(per_page, LOG_PAGE_MAX))
    total = db.scalar(select(func.count(SystemLogEntry.id)).where(*conditions)) or 0
    items = db.scalars(select(SystemLogEntry).where(*conditions)
                       .order_by(SystemLogEntry.created_at.desc(), SystemLogEntry.id.desc())
                       .offset((page - 1) * per_page).limit(per_page)).all()
    # Like the API calls table, the choices come from the range rather than from the rows already filtered —
    # and from the range rather than the page, so a provider on page two is still selectable from page one.
    in_range = [SystemLogEntry.created_at >= start, SystemLogEntry.created_at <= end,
                *([] if user_id is None else [SystemLogEntry.user_id == user_id])]
    choices = db.execute(select(SystemLogEntry.category, SystemLogEntry.provider, SystemLogEntry.workflow)
                         .where(*in_range).distinct().limit(LOG_PAGE_MAX * 20)).all()
    return {"items": [_log_card(item) for item in items], "total": total, "page": page, "per_page": per_page,
            "pages": max(1, -(-total // per_page)),
            "categories": sorted({row[0] for row in choices if row[0]}),
            "providers": sorted({row[1] for row in choices if row[1]}),
            "workflows": sorted({row[2] for row in choices if row[2]})}


def _log_card(item: SystemLogEntry) -> dict[str, Any]:
    return {"id": item.id, "created_at": item.created_at.isoformat(), "level": item.level,
            "category": item.category, "provider": item.provider, "operation": item.operation,
            "workflow": item.workflow, "trace_id": item.trace_id, "entity_type": item.entity_type,
            "entity_id": item.entity_id, "message": item.message, "status": item.status,
            "duration_ms": item.duration_ms, "external_request_id": item.external_request_id,
            "details": item.details}


def log_detail(db: Session, log_id: int, user_id: int | None) -> dict[str, Any] | None:
    item = db.get(SystemLogEntry, log_id)
    if item is None or (user_id is not None and item.user_id not in (None, user_id)):
        return None
    external = db.scalar(select(Row).where(Row.request_id == item.external_request_id)) \
        if item.external_request_id else None
    llm_requests = _llm_requests_for(db, item, user_id)
    if external is None and len(llm_requests) == 1 and llm_requests[0].get("external_request_id"):
        # A line about one LLM call: its provider request is where the raw error body (if captured) lives.
        external = db.scalar(select(Row).where(Row.request_id == llm_requests[0]["external_request_id"]))
    if external is not None and user_id is not None and external.user_id not in (None, user_id):
        external = None
    return {**_log_card(item),
            "external_request": {**row_card(external), "payload": captured(external)} if external is not None else None,
            "llm_requests": llm_requests,
            "related": [_log_card(other) for other in db.scalars(
                select(SystemLogEntry).where(SystemLogEntry.trace_id == item.trace_id, SystemLogEntry.id != item.id)
                .order_by(SystemLogEntry.id).limit(20)).all()] if item.trace_id else []}


def _llm_requests_for(db: Session, item: SystemLogEntry, user_id: int | None) -> list[dict[str, Any]]:
    """The LLM requests a log line is about: the one behind its external call, else those in its trace. Each opens
    in Monitoring → LLM requests, where the full prompt and response are when they were captured."""
    from app.models import LLMRequestLog

    exact = (item.details or {}).get("llm_request_id")
    if exact:  # a line written for one LLM call (app.services.llm_telemetry.log_call)
        query = select(LLMRequestLog).where(LLMRequestLog.request_id == str(exact))
    elif item.external_request_id:
        query = select(LLMRequestLog).where(LLMRequestLog.external_request_id == item.external_request_id)
    elif item.trace_id:
        query = select(LLMRequestLog).where(LLMRequestLog.trace_id == item.trace_id)
    else:
        return []
    if user_id is not None:
        query = query.where(LLMRequestLog.user_id == user_id)
    rows_ = db.scalars(query.order_by(LLMRequestLog.id).limit(20)).all()
    return [{"id": r.id, "stage": r.stage, "model": r.model or r.requested_model, "status": r.status,
             "external_request_id": r.external_request_id,
             "created_at": r.created_at.isoformat(), "captured": bool(r.debug_prompt or r.debug_response),
             # The line is about exactly this call: its prompt and response come with it, redacted again on read.
             "payload": _payload(r) if len(rows_) == 1 else None}
            for r in rows_]


def _payload(row: Any) -> dict[str, str] | None:
    from app.services.monitoring import DEBUG_TEXT_LIMIT
    from app.services.redaction import redact_text

    if not (row.debug_prompt or row.debug_response):
        return None
    return {"prompt": redact_text(row.debug_prompt, DEBUG_TEXT_LIMIT) or "",
            "response": redact_text(row.debug_response, DEBUG_TEXT_LIMIT) or ""}


def trace_detail(db: Session, trace_id: str, user_id: int | None) -> dict[str, Any]:
    """Everything one correlation ID touched: the calls it made and the events it logged, in order."""
    from app.models import LLMRequestLog

    calls = select(Row).where(Row.trace_id == trace_id)
    events = select(SystemLogEntry).where(SystemLogEntry.trace_id == trace_id)
    llm = select(LLMRequestLog).where(LLMRequestLog.trace_id == trace_id)
    if user_id is not None:
        calls, events, llm = (calls.where(Row.user_id == user_id), events.where(SystemLogEntry.user_id == user_id),
                              llm.where(LLMRequestLog.user_id == user_id))
    rows_ = list(db.scalars(calls.order_by(Row.created_at).limit(500)).all())
    return {"trace_id": trace_id,
            "calls": [row_card(r) for r in rows_],
            "events": [_log_card(e) for e in db.scalars(events.order_by(SystemLogEntry.created_at).limit(200)).all()],
            "llm_requests": [{"id": r.id, "stage": r.stage, "model": r.model, "status": r.status,
                              "latency_ms": r.latency_ms, "estimated_cost": _money(r.estimated_cost),
                              "created_at": r.created_at.isoformat()}
                             for r in db.scalars(llm.order_by(LLMRequestLog.created_at).limit(200)).all()],
            "providers": sorted({r.provider for r in rows_}),
            "total_latency_ms": sum(r.latency_ms or 0 for r in rows_)}


# ───────────── the dashboard, and what the advisor is given ─────────────

def facets(db: Session, f: ExternalFilter) -> dict[str, list[str]]:
    """Everything that can be filtered for in this range, whatever is filtered for now.

    Options must not come from the filtered rows: picking one provider would then remove every other provider
    from the list, leaving no way back except Reset. So this looks at the range and the account only.
    """
    unfiltered = replace(f, provider=None, service=None, operation=None, workflow=None, status=None,
                         error_only=False, error_type=None, http_status_code=None, job_id=None,
                         application_id=None, trace_id=None)
    columns = db.execute(select(Row.provider, Row.service, Row.operation, Row.workflow, Row.error_type,
                                Row.http_status_code)
                         .where(Row.created_at >= unfiltered.start, Row.created_at <= unfiltered.end,
                                *([] if unfiltered.user_id is None else [Row.user_id == unfiltered.user_id]))
                         .distinct().limit(MAX_ROWS)).all()
    def values(index: int) -> list[str]:
        return sorted({str(row[index]) for row in columns if row[index] not in (None, "")})

    return {"providers": values(0), "services": values(1), "operations": values(2), "workflows": values(3),
            "error_types": values(4), "status_codes": sorted({row[5] for row in columns if row[5] is not None})}


def dashboard(db: Session, f: ExternalFilter) -> dict[str, Any]:
    items = rows(db, f)
    return {"filter": f.public(), "summary": summary(items), "providers": by_provider(items),
            "operations": by_operation(items)[:25], "workflows": by_workflow(items),
            "errors": errors(items), "timeseries": timeseries(items, f), "latency": latency(items),
            "apify": apify_summary(db, f, items), "facets": facets(db, f),
            # Providers this account is currently backing off from (app.services.backoff), and what that saved.
            "cooldowns": backoff.active(db, f.user_id) if f.user_id is not None else []}


def advisor_aggregates(db: Session, user_id: int, start: datetime, end: datetime) -> dict[str, Any]:
    """A compact, deterministic summary of one account's third-party usage for the AI Optimization Advisor.

    Metadata only: counts, rates, latency percentiles, retries and reported cost. No URLs, no payloads.
    """
    f = ExternalFilter(start=start, end=end, user_id=user_id)
    items = rows(db, f)
    fields = ("requests", "failed", "success_rate", "failure_rate", "rate_limited", "rate_limit_rate",
              "server_errors", "timeouts", "timeout_rate", "connection_errors", "retries", "retry_rate",
              "avg_latency_ms", "p95_latency_ms", "p99_latency_ms", "estimated_cost")
    apify = apify_summary(db, f, items)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": max(1, (end - start).days)},
        "totals": {k: summary(items)[k] for k in fields},
        "providers": [{"provider": row["provider"], **{k: row[k] for k in fields}} for row in by_provider(items)],
        "operations": [{k: row[k] for k in ("provider", "service", "operation", *fields)}
                       for row in by_operation(items)[:25]],
        "workflows": [{"workflow": row["workflow"], **{k: row[k] for k in fields}} for row in by_workflow(items)],
        "errors": errors(items)["by_type"],
        "apify": {k: apify[k] for k in ("runs", "succeeded", "failed", "success_rate", "compute_units",
                                        "reported_cost", "jobs_discovered", "jobs_after_deduplication",
                                        "dataset_items", "runs_with_reported_cost")},
        "apify_by_actor": apify["by_actor"][:10],
        "apify_by_saved_search": apify["by_saved_search"][:10],
    }


# ───────────── retention ─────────────

PRUNE_BATCH = 5000


def clear_captured(db: Session, now: datetime | None = None, days: int | None = None) -> int:
    """Forget captured request/response bodies older than the capture retention (the row and its numbers stay).
    Bounded: one batch per run. Commits."""
    from sqlalchemy import update

    from app.settings import get_settings

    keep = days if days is not None else get_settings().llm_prompt_debug_retention_days
    cutoff = (now or utcnow()) - timedelta(days=max(0, keep))
    ids = list(db.scalars(select(Row.id).where(Row.payload.is_not(None), Row.created_at < cutoff)
                          .order_by(Row.id).limit(PRUNE_BATCH)).all())
    if ids:
        db.execute(update(Row).where(Row.id.in_(ids)).values(payload=None).execution_options(synchronize_session=False))
        db.commit()
    return len(ids)


def prune(db: Session, now: datetime | None = None, retention_days: int | None = None) -> dict[str, int]:
    """Roll raw call rows older than the retention into daily totals, then delete them. Commits.

    Daily totals (external_api_usage_daily) are kept indefinitely: counts, failure kinds, latency and cost only.
    """
    from sqlalchemy import delete

    from app.models import ExternalAPIUsageDaily
    from app.settings import get_settings

    days = retention_days if retention_days is not None else get_settings().external_api_log_retention_days
    if days <= 0:
        return {"rolled_up": 0, "deleted": 0}
    cutoff = (now or utcnow()) - timedelta(days=days)
    old = db.execute(select(*ROW_COLUMNS).where(Row.created_at < cutoff)
                     .order_by(Row.id).limit(PRUNE_BATCH)).all()
    if not old:
        return {"rolled_up": 0, "deleted": 0}
    groups: dict[tuple, list[Any]] = defaultdict(list)
    for r in old:
        groups[(r.user_id, r.created_at.date(), r.provider, r.service or "", r.operation or "")].append(r)
    for (user_id, day, provider, service, operation), group in groups.items():
        row = db.scalar(select(ExternalAPIUsageDaily).where(
            ExternalAPIUsageDaily.user_id.is_(None) if user_id is None else ExternalAPIUsageDaily.user_id == user_id,
            ExternalAPIUsageDaily.day == day, ExternalAPIUsageDaily.provider == provider,
            ExternalAPIUsageDaily.service == service, ExternalAPIUsageDaily.operation == operation))
        if row is None:
            row = ExternalAPIUsageDaily(user_id=user_id, day=day, provider=provider, service=service,
                                        operation=operation, requests=0, failures=0, retries=0, rate_limited=0,
                                        server_errors=0, timeouts=0, latency_ms_sum=0, latency_ms_max=0,
                                        request_bytes=0, response_bytes=0, estimated_cost=None, unpriced_requests=0)
            db.add(row)
        costs = [r.estimated_cost for r in group if r.estimated_cost is not None]
        row.requests += len(group)
        row.failures += sum(r.status == "FAILED" for r in group)
        row.retries += sum(r.retry_count or 0 for r in group)
        row.rate_limited += sum(r.error_type == "RATE_LIMIT" for r in group)
        row.server_errors += sum(r.error_type == "SERVER_ERROR" for r in group)
        row.timeouts += sum(r.error_type == "TIMEOUT" for r in group)
        row.latency_ms_sum += sum(r.latency_ms or 0 for r in group)
        row.latency_ms_max = max([row.latency_ms_max, *[r.latency_ms or 0 for r in group]])
        row.request_bytes += sum(r.request_size_bytes or 0 for r in group)
        row.response_bytes += sum(r.response_size_bytes or 0 for r in group)
        if costs:
            row.estimated_cost = (row.estimated_cost or Decimal(0)) + sum(costs, Decimal(0))
        row.unpriced_requests += len(group) - len(costs)
    deleted = db.execute(delete(Row).where(Row.id.in_([r.id for r in old]))
                         .execution_options(synchronize_session=False)).rowcount or 0
    db.commit()
    return {"rolled_up": len(groups), "deleted": deleted}


__all__ = ["ExternalFilter", "advisor_aggregates", "apify_summary", "by_operation", "by_provider",
           "conditions", "dashboard", "facets",
           "errors", "health", "latency", "list_requests", "log_detail", "logs", "prune", "request_detail",
           "row_card", "run_card", "rows", "summary", "time_range", "timeseries", "trace_detail"]
