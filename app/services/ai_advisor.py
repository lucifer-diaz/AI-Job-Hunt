"""AI Optimization Advisor: asynchronous analysis of an account's LLM telemetry into recommendations.

    POST /api/monitoring/analyze or a daily/weekly schedule → AIOptimizationTask (PENDING)
    → scheduler job `ai_optimization` claims it (conditional UPDATE + lease, safe across app instances)
    → monitoring.advisor_aggregates: deterministic, compact, no prompt text
    → detect_findings: deterministic rules (context, cost, model, reliability, latency, prompt, duplicate calls)
    → when the account's AI provider is usable: build_ai_optimization_prompt → one validated LLM call
      (otherwise, or when the answer fails validation, the report is built from the rule findings)
    → AIOptimizationReport (COMPLETED)

ADVICE ONLY. This module reads telemetry and writes nothing but its own tasks and reports. It never changes
model configuration, prompts, candidate policy, thresholds, API keys, preferences or any other configuration;
tests enforce that.
"""
from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AIOptimizationReport, AIOptimizationTask, AppSetting
from app.prompts import contracts
from app.prompts.ai_optimization import (
    AI_OPTIMIZATION_PROMPT_VERSION,
    build_ai_optimization_prompt,
)
from app.services import external_monitoring, monitoring, prompt_budget
from app.services.llm import LLMCall, LLMClient
from app.services.user_settings import ai_ready

log = logging.getLogger("jobhunt.ai_advisor")
SessionFactory = Callable[[], Session]
PENDING, PROCESSING, COMPLETED, FAILED = "PENDING", "PROCESSING", "COMPLETED", "FAILED"
ACTIVE = (PENDING, PROCESSING)
SCHEDULES = {"off": None, "daily": timedelta(days=1), "weekly": timedelta(days=7)}
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3
MAX_RECOMMENDATIONS = 10

# Rule thresholds. A stage needs MIN_REQUESTS requests before its patterns count as findings.
MIN_REQUESTS = 5
CONTEXT_CONTRIBUTOR_SHARE = 0.5
CONTEXT_MIN_TOKENS = 2000
HIGH_UTILIZATION_PERCENT = 50
STAGE_COST_SHARE = 0.35
SCHEMA_FAILURE_GAP = 0.02
RETRY_RATE = 0.10
FAILURE_RATE = 0.05
P95_LATENCY_MS = 30_000
OUTPUT_INPUT_RATIO = 0.05
PROMPT_HEAVY_INPUT_TOKENS = 5000
SYSTEM_PROMPT_SHARE = 0.30
FINDING_GROUPS = {"COST": "cost_findings", "ARCHITECTURE": "cost_findings", "LATENCY": "latency_findings",
                  "CONTEXT": "context_findings", "MODEL": "model_findings", "PROMPT": "prompt_findings",
                  "RELIABILITY": "reliability_findings"}
CONTRIBUTOR_FIX = {
    "evidence": "Retrieve only the verified evidence relevant to the posting instead of the whole Evidence Bank.",
    "resume": "Send only the resume sections relevant to the job (selected roles and bullets), not the full text.",
    "job_description": "Strip boilerplate (benefits, EEO statements, company history) from job descriptions first.",
    "candidate_profile": "Send only the profile fields this stage uses.",
    "conversation": "Send only the latest messages or a short summary of earlier ones.",
    "research": "Summarise company research before sending it.",
    "system_prompt": "Shorten and deduplicate the system instructions.",
    "other": "Review which untagged prompt sections are needed.",
}


class ActiveAnalysis(RuntimeError):
    """An analysis is already waiting or running for this account."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _title(stage: str) -> str:
    return stage.replace("_", " ").title()


# ───────────── queueing ─────────────

def queue_analysis(db: Session, user_id: int, start: datetime, end: datetime, trigger: str = "manual") -> AIOptimizationTask:
    """Queue one analysis. Raises ActiveAnalysis when one is already waiting or running. Commits."""
    active = db.scalar(select(AIOptimizationTask).where(AIOptimizationTask.user_id == user_id,
                                                        AIOptimizationTask.status.in_(ACTIVE)))
    if active is not None:
        raise ActiveAnalysis(f"An analysis is already {active.status.lower()}")
    task = AIOptimizationTask(user_id=user_id, time_range_start=start, time_range_end=end, status=PENDING,
                              trigger=trigger, attempt_count=0, created_at=utcnow())
    db.add(task)
    try:
        db.commit()
    except IntegrityError as exc:  # queued concurrently
        db.rollback()
        raise ActiveAnalysis("An analysis is already pending") from exc
    return task


def schedule_of(db: Session, user_id: int) -> str:
    row = db.scalar(select(AppSetting.preferences).where(AppSetting.user_id == user_id))
    value = ((row or {}).get("ai_advisor") or {}).get("schedule")
    return value if value in SCHEDULES else "off"


def enqueue_scheduled(db: Session, now: datetime | None = None) -> int:
    """Queue analyses for accounts whose daily or weekly schedule is due. Reads preferences, never writes them."""
    now = now or utcnow()
    queued = 0
    for user_id, preferences in db.execute(select(AppSetting.user_id, AppSetting.preferences)).all():
        schedule = ((preferences or {}).get("ai_advisor") or {}).get("schedule")
        period = SCHEDULES.get(schedule) if schedule in SCHEDULES else None
        if period is None or user_id is None:
            continue
        latest = db.scalar(select(AIOptimizationTask.created_at).where(AIOptimizationTask.user_id == user_id)
                           .order_by(AIOptimizationTask.created_at.desc()).limit(1))
        if latest is not None and latest > now - period:
            continue
        try:
            queue_analysis(db, user_id, now - period, now, trigger="scheduled")
            queued += 1
        except ActiveAnalysis:
            continue
    return queued


# ───────────── claiming ─────────────

def recover_expired(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    expired = (AIOptimizationTask.status == PROCESSING) & (AIOptimizationTask.lock_expires_at < now)
    failed = db.execute(update(AIOptimizationTask).where(expired, AIOptimizationTask.attempt_count >= MAX_ATTEMPTS)
                        .values(status=FAILED, completed_at=now, locked_by=None, lock_token=None, lock_expires_at=None,
                                error="The analysis was interrupted too many times")
                        .execution_options(synchronize_session=False)).rowcount or 0
    retried = db.execute(update(AIOptimizationTask).where(expired)
                         .values(status=PENDING, locked_by=None, lock_token=None, lock_expires_at=None,
                                 next_attempt_at=now).execution_options(synchronize_session=False)).rowcount or 0
    db.commit()
    return failed + retried


@dataclass(frozen=True)
class Claim:
    task_id: int
    user_id: int
    token: str
    worker_id: str


def claim_next(sessions: SessionFactory, worker_id: str, now: datetime | None = None) -> Claim | None:
    """Take the oldest due PENDING task with a conditional UPDATE, so two instances never run the same analysis."""
    now = now or utcnow()
    with sessions() as db:
        due = or_(AIOptimizationTask.next_attempt_at.is_(None), AIOptimizationTask.next_attempt_at <= now)
        for task_id, user_id in db.execute(select(AIOptimizationTask.id, AIOptimizationTask.user_id)
                                           .where(AIOptimizationTask.status == PENDING, due)
                                           .order_by(AIOptimizationTask.id).limit(10)).all():
            token = uuid.uuid4().hex
            won = db.execute(update(AIOptimizationTask).where(AIOptimizationTask.id == task_id,
                                                              AIOptimizationTask.status == PENDING)
                             .values(status=PROCESSING, locked_by=worker_id[:200], lock_token=token,
                                     lock_expires_at=now + LEASE, started_at=now,
                                     attempt_count=AIOptimizationTask.attempt_count + 1)
                             .execution_options(synchronize_session=False)).rowcount
            db.commit()
            if won:
                return Claim(task_id, user_id, token, worker_id)
    return None


# ───────────── deterministic findings ─────────────

def _finding(category: str, priority: str, stage: str, finding: str, evidence: str, change: str, benefit: str,
             saving: str, risk: str) -> dict[str, Any]:
    return {"category": category, "priority": priority, "affected_stage": stage, "finding": finding,
            "evidence": evidence, "recommendation": {
                "title": change.split(":")[0] if ":" in change[:60] else change[:90], "category": category,
                "priority": priority, "finding": finding, "evidence": evidence, "recommended_change": change,
                "expected_benefit": benefit, "estimated_cost_saving": saving, "risk": risk, "affected_stage": stage}}


def detect_findings(aggregates: dict[str, Any]) -> list[dict[str, Any]]:
    """Patterns in the aggregates that merit a recommendation. Pure and deterministic."""
    findings: list[dict[str, Any]] = []
    stages = [s for s in aggregates.get("stages", []) if s["requests"] >= MIN_REQUESTS]
    for s in stages:
        stage, name = s["stage"], _title(s["stage"])
        contributors = s.get("context_contributors") or {}
        top = max(contributors.items(), key=lambda kv: kv[1], default=None)
        if top and top[1] > CONTEXT_CONTRIBUTOR_SHARE and (s["avg_context_tokens"] or 0) >= CONTEXT_MIN_TOKENS:
            label = top[0].replace("_", " ")
            findings.append(_finding(
                "CONTEXT", "HIGH" if top[1] > 0.7 else "MEDIUM", stage,
                f"{name} context is dominated by {label}.",
                f"{label.capitalize()} is {_pct(top[1])} of {name} context; average context {s['avg_context_tokens']:,.0f} tokens "
                f"(p95 {s['p95_context_tokens'] or 0:,.0f}) over {s['requests']} requests.",
                f"Reduce {label} in {name} prompts: {CONTRIBUTOR_FIX.get(top[0], '')}",
                "Lower input-token cost and latency.", f"up to {top[1] * 50:.0f}% of {name} input tokens",
                "Low if relevance filtering keeps the facts the model needs; watch ranking quality."))
        if (s.get("avg_context_utilization_percent") or 0) > HIGH_UTILIZATION_PERCENT:
            findings.append(_finding(
                "CONTEXT", "HIGH", stage, f"{name} requests use a large share of the model's context window.",
                f"Average context utilisation {s['avg_context_utilization_percent']:.1f}%.",
                f"Trim {name} prompts or move the stage to a model with a larger context window.",
                "Fewer context-overflow failures and truncated inputs.", "unknown",
                "Trimming may drop useful facts; verify output quality."))
        if contributors.get("system_prompt", 0) > SYSTEM_PROMPT_SHARE and (s["avg_context_tokens"] or 0) >= 1000:
            findings.append(_finding(
                "PROMPT", "LOW", stage, f"{name} system instructions are a large part of every request.",
                f"System prompt is {_pct(contributors['system_prompt'])} of {name} context.",
                f"Shorten the {name} system prompt: remove repeated rules and examples.",
                "Lower input cost on every call.", f"up to {contributors['system_prompt'] * 30:.0f}% of input tokens",
                "Low; re-run prompt tests after editing."))
        if (s["input_tokens"] or 0) / max(1, s["requests"]) >= PROMPT_HEAVY_INPUT_TOKENS \
                and s["output_tokens"] < s["input_tokens"] * OUTPUT_INPUT_RATIO:
            findings.append(_finding(
                "PROMPT", "MEDIUM", stage, f"{name} sends far more input than it needs for its output.",
                f"{s['input_tokens']:,} input vs {s['output_tokens']:,} output tokens "
                f"({_pct(s['output_tokens'] / max(1, s['input_tokens']))}).",
                f"Send {name} only the inputs its output depends on.",
                "Lower input-token cost.", "unknown", "Low if outputs are compared before and after."))
        if s.get("cost_share") and s["cost_share"] > STAGE_COST_SHARE:
            change = ("Run deterministic validation first and call the Reviewer only when it is needed."
                      if stage == "REVIEWER" else f"Move {name} to a cheaper model or reduce how often it runs.")
            findings.append(_finding(
                "COST", "HIGH" if s["cost_share"] > 0.5 else "MEDIUM", stage, f"{name} dominates AI spend.",
                f"{name} is {_pct(s['cost_share'])} of estimated cost (${s['estimated_cost'] or 0:,.4f}).",
                change, "Lower total AI spend.", f"part of {_pct(s['cost_share'])} of spend",
                "A cheaper model may lower quality; compare outputs on a sample first."))
        if (s.get("retry_rate") or 0) > RETRY_RATE:
            findings.append(_finding(
                "RELIABILITY", "MEDIUM", stage, f"{name} often needs a second call.",
                f"{_pct(s['retry_rate'])} of {name} requests are retries "
                f"(structured-output failure rate {_pct(s.get('structured_output_failure_rate'))}).",
                f"Investigate {name} retries: prompt ambiguity, schema complexity, model choice, context size, "
                "provider reliability.", "Fewer wasted calls and faster results.",
                f"up to {_pct(s['retry_rate'])} of {name} calls", "None; investigation only."))
        if (s.get("failure_rate") or 0) > FAILURE_RATE:
            findings.append(_finding(
                "RELIABILITY", "HIGH" if s["failure_rate"] > 0.2 else "MEDIUM", stage, f"{name} requests fail often.",
                f"{_pct(s['failure_rate'])} of {s['requests']} {name} requests failed.",
                f"Check {name} failures by type on the Monitoring tab and add fallbacks for the dominant cause.",
                "More jobs processed on the first attempt.", "unknown", "Low."))
        if (s.get("p95_latency_ms") or 0) > P95_LATENCY_MS:
            findings.append(_finding(
                "LATENCY", "MEDIUM", stage, f"{name} is slow at the tail.",
                f"p95 latency {s['p95_latency_ms'] / 1000:.1f}s (average {(s['avg_latency_ms'] or 0) / 1000:.1f}s).",
                f"Reduce {name} context or use a faster model for it.", "Faster pipeline turnaround.", "unknown",
                "Faster models may be less accurate."))
        stage_models = [m for m in aggregates.get("stage_models", []) if m["stage"] == stage]
        free = [m for m in stage_models if m["free"] and m["requests"] >= MIN_REQUESTS]
        paid = [m for m in stage_models if not m["free"] and (m["estimated_cost"] or 0) > 0]
        for p in paid:
            for f in free:
                if (f["structured_output_failure_rate"] or 0) <= (p["structured_output_failure_rate"] or 0) + SCHEMA_FAILURE_GAP:
                    findings.append(_finding(
                        "MODEL", "MEDIUM", stage, f"{name} uses a paid model where a free model already works.",
                        f"{p['model']} cost ${p['estimated_cost']:,.4f}; free {f['model']} handled {f['requests']} "
                        f"requests with structured-output failure rate {_pct(f['structured_output_failure_rate'])} "
                        f"vs {_pct(p['structured_output_failure_rate'])}.",
                        f"Consider moving {name} from {p['model']} to {f['model']} after a quality check.",
                        "Lower cost.", f"up to ${p['estimated_cost']:,.4f} over this period",
                        "Output quality may differ; compare a sample before switching."))
    models = [m for m in aggregates.get("models", []) if m.get("structured_output_failure_rate") is not None
              and m["requests"] >= 10]
    if len(models) >= 2:
        best = min(models, key=lambda m: m["structured_output_failure_rate"])
        for m in models:
            gap = m["structured_output_failure_rate"] - best["structured_output_failure_rate"]
            if m is not best and gap > SCHEMA_FAILURE_GAP:
                shared = sorted(set(m["stages"]) & set(best["stages"]))
                findings.append(_finding(
                    "RELIABILITY", "MEDIUM", shared[0] if len(shared) == 1 else "ALL",
                    f"{m['model']} fails structured output more often than {best['model']}.",
                    f"{m['model']}: {_pct(m['structured_output_failure_rate'])} schema failures; "
                    f"{best['model']}: {_pct(best['structured_output_failure_rate'])}.",
                    f"Prefer {best['model']} for structured requests.", "Fewer retries and failed jobs.", "unknown",
                    "Cost and latency may differ between the models."))
    for item in aggregates.get("duplicate_successful_calls", []):
        findings.append(_finding(
            "ARCHITECTURE", "MEDIUM" if item["extra_calls"] >= 5 else "LOW", item["stage"],
            f"{_title(item['stage'])} repeats successful calls for the same item.",
            f"{item['extra_calls']} extra successful calls with the same entity, stage and prompt version.",
            "Reuse stored results when the inputs have not changed.", "Fewer unnecessary LLM calls.",
            f"{item['extra_calls']} calls", "Low if reuse checks input hashes."))
    unpriced = (aggregates.get("totals") or {}).get("unpriced_requests") or 0
    if unpriced:
        findings.append(_finding(
            "COST", "LOW", "ALL", "Some requests have no cost estimate.",
            f"{unpriced} requests used models without pricing in app/model_catalog.json.",
            "Add verified prices for those models to app/model_catalog.json.",
            "Complete cost visibility, so spend comparisons are accurate.", "unknown", "None."))
    findings += detect_external_findings(aggregates.get("external") or {})
    findings += detect_routing_findings(aggregates.get("routing") or {})
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(findings, key=lambda f: order[f["priority"]])


# Rule thresholds for model routing (LiteLLM gateway mode). Rates are shares of that worker's own requests.
ROUTING_MIN_REQUESTS = 10
ESCALATION_HIGH = 0.5
ESCALATION_KEEP = 0.1
INFERENCE_SHARE = 0.3
ROUTING_SCHEMA_FAILURE = 0.1
ROUTING_FALLBACK_RATE = 0.1


def detect_routing_findings(routing: dict[str, Any]) -> list[dict[str, Any]]:
    """Recommendations about which model each worker should use, from routing telemetry. Pure and deterministic.

    Advice only: a finding may say "assign PRIMARY_REASONING directly", but nothing here (or anywhere in the
    advisor) changes a worker's model, a tier, the auto router or any other configuration.
    """
    from app.services.ai_workers import MONITORING_STAGE
    from app.services.ai_workers import label as worker_label

    findings: list[dict[str, Any]] = []
    for w in routing.get("workers") or []:
        if w["requests"] < ROUTING_MIN_REQUESTS:
            continue
        name, stage = worker_label(w["worker_type"]), MONITORING_STAGE.get(w["worker_type"], "OTHER")
        configured = ", ".join(w.get("configured") or []) or "its configured model"
        escalation = w.get("escalation_rate")
        if escalation is not None and escalation >= ESCALATION_HIGH:
            findings.append(_finding(
                "MODEL", "HIGH" if escalation >= 0.6 else "MEDIUM", stage,
                f"{_pct(escalation)} of {name} requests escalate from {configured}.",
                f"{w['escalations']} escalations over {w['requests']} {name} requests.",
                f"Assign {name} its escalation model (or PRIMARY_REASONING) directly in AI Orchestration → Models.",
                "One request instead of two for most calls: lower latency and less inference time.",
                "unknown", "Low: the escalation model already answers these requests today."))
        elif escalation is not None and escalation <= ESCALATION_KEEP and (w.get("schema_failure_rate") or 0) \
                <= SCHEMA_FAILURE_GAP and (w.get("failure_rate") or 0) <= FAILURE_RATE:
            findings.append(_finding(
                "MODEL", "LOW", stage,
                f"{_pct(1 - escalation)} of {name} requests succeed on {configured} without escalation.",
                f"Escalation rate {_pct(escalation)}, schema failure rate {_pct(w.get('schema_failure_rate'))} "
                f"over {w['requests']} requests.",
                f"Keep the current {name} configuration.", "Stable quality at the current cost.", "none",
                "None."))
        share = w.get("inference_share")
        if share is not None and share >= INFERENCE_SHARE:
            findings.append(_finding(
                "ARCHITECTURE", "MEDIUM", stage, f"{name} uses {_pct(share)} of all inference time.",
                f"{name}: {w['requests']} requests, p95 latency {w.get('p95_latency_ms')} ms.",
                f"Consider a faster model (e.g. PRIMARY_REASONING) for {name}'s first pass, with a quality-escalation "
                "policy (low confidence or schema failure) sending only uncertain cases to the larger model.",
                "Less local compute and lower latency for the common case.", "unknown",
                "Medium: measure answer quality before and after; keep the larger model for uncertain cases."))
        fallback = w.get("fallback_rate")
        if fallback is not None and fallback >= ROUTING_FALLBACK_RATE:
            findings.append(_finding(
                "RELIABILITY", "MEDIUM", stage,
                f"{_pct(fallback)} of {name} requests were answered by the fallback model.",
                f"LiteLLM reported a fallback on {_pct(fallback)} of {name} requests.",
                f"Check the health of {name}'s primary model (AI Orchestration → Models → Test) before changing it.",
                "Predictable quality: the configured model answers, not its fallback.", "unknown", "Low."))
    for x in routing.get("worker_models") or []:
        rate = x.get("schema_failure_rate")
        if x["requests"] >= ROUTING_MIN_REQUESTS and rate is not None and rate >= ROUTING_SCHEMA_FAILURE:
            name = worker_label(x["worker_type"])
            findings.append(_finding(
                "RELIABILITY", "HIGH" if rate >= 2 * ROUTING_SCHEMA_FAILURE else "MEDIUM",
                MONITORING_STAGE.get(x["worker_type"], "OTHER"),
                f"{x['model']} returns invalid structured output for {name} {_pct(rate)} of the time.",
                f"{_pct(rate)} schema failures over {x['requests']} {name} requests on {x['model']}.",
                f"Add a SCHEMA_FAILURE escalation for {name}, or assign it a model that returns JSON reliably.",
                "Fewer failed and retried requests.", "unknown", "Low."))
    return findings


# Rule thresholds for third-party APIs. Rates are shares of that provider's or actor's own calls.
EXTERNAL_MIN_REQUESTS = 10
EXTERNAL_FAILURE_RATE = 0.10
EXTERNAL_RATE_LIMIT_RATE = 0.05
EXTERNAL_RETRY_RATE = 0.15
LATENCY_MULTIPLE = 2.5
APIFY_MIN_RUNS = 5
APIFY_FAILURE_RATE = 0.25
APIFY_JOBS_PER_RUN = 1.0


def detect_external_findings(external: dict[str, Any]) -> list[dict[str, Any]]:
    """Patterns in third-party API telemetry that merit a recommendation. Pure and deterministic.

    Every rule is a ratio over that provider's own calls, so a provider used twice cannot raise an alarm.
    """
    findings: list[dict[str, Any]] = []
    providers = [p for p in external.get("providers", []) if p["requests"] >= EXTERNAL_MIN_REQUESTS]
    for p in providers:
        name = p["provider"]
        if (p["failure_rate"] or 0) >= EXTERNAL_FAILURE_RATE:
            findings.append(_finding(
                "RELIABILITY", "HIGH" if (p["failure_rate"] or 0) >= 2 * EXTERNAL_FAILURE_RATE else "MEDIUM", "ALL",
                f"{name} calls fail often.",
                f"{p['failed']} of {p['requests']} {name} calls failed ({_pct(p['failure_rate'])}); "
                f"{p['timeouts']} timed out, {p['server_errors']} returned 5xx.",
                f"Check the {name} integration and its timeouts before adding more load.",
                "Fewer lost searches and less repeated work.", "unknown",
                "None: this is a diagnosis, not a change."))
        if (p["rate_limit_rate"] or 0) >= EXTERNAL_RATE_LIMIT_RATE:
            findings.append(_finding(
                "RELIABILITY", "MEDIUM", "ALL", f"{name} is rate limiting this account.",
                f"{p['rate_limited']} of {p['requests']} {name} calls were rate limited ({_pct(p['rate_limit_rate'])}).",
                f"Slow the schedule that drives {name}, or spread its calls out.",
                "Fewer rejected calls and less retry waste.", "unknown", "Work is discovered more slowly."))
        if (p["retry_rate"] or 0) >= EXTERNAL_RETRY_RATE:
            findings.append(_finding(
                "COST", "MEDIUM", "ALL", f"Retries are a large share of {name} calls.",
                f"{p['retries']} retries across {p['requests']} {name} calls ({_pct(p['retry_rate'])} were retries).",
                f"Find why {name} calls are being repeated before increasing the retry limit.",
                "Less duplicated work and lower spend.", "unknown", "None."))
    # The same workflow going to two providers: one being markedly slower is worth knowing about.
    by_operation = [o for o in external.get("operations", []) if o["requests"] >= EXTERNAL_MIN_REQUESTS
                    and o["p95_latency_ms"]]
    for operation in {o["operation"] for o in by_operation}:
        peers = sorted((o for o in by_operation if o["operation"] == operation), key=lambda o: o["p95_latency_ms"])
        if len(peers) > 1 and peers[-1]["p95_latency_ms"] >= LATENCY_MULTIPLE * peers[0]["p95_latency_ms"]:
            slow, fast = peers[-1], peers[0]
            findings.append(_finding(
                "LATENCY", "MEDIUM", "ALL", f"{slow['provider']} is much slower at {operation}.",
                f"p95 {slow['p95_latency_ms']:.0f}ms on {slow['provider']} against "
                f"{fast['p95_latency_ms']:.0f}ms on {fast['provider']} for the same operation.",
                f"Prefer {fast['provider']} for {operation} where both can do the work.",
                "Shorter end-to-end runs.", "unknown", "The providers may not return the same quality of data."))
    apify = external.get("apify") or {}
    for actor in external.get("apify_by_actor", []):
        runs = actor["runs"]
        if runs >= APIFY_MIN_RUNS and actor["failed"] / runs >= APIFY_FAILURE_RATE:
            findings.append(_finding(
                "RELIABILITY", "HIGH", "ALL", f"Apify actor {actor['actor_id']} fails often.",
                f"{actor['failed']} of {runs} runs of {actor['actor_id']} did not succeed.",
                "Review that actor's inputs, or replace it with another actor for the same boards.",
                "More searches that actually return jobs.", "unknown",
                "A different actor may cover different boards."))
        if runs >= APIFY_MIN_RUNS and actor["jobs_created"] / runs < APIFY_JOBS_PER_RUN:
            findings.append(_finding(
                "COST", "MEDIUM", "ALL", f"Apify actor {actor['actor_id']} rarely finds new jobs.",
                f"{runs} runs produced {actor['jobs_created']} new jobs after deduplication"
                + (f", using {actor['compute_units']} compute units" if actor["compute_units"] is not None else "")
                + (f" and ${actor['reported_cost']}" if actor["reported_cost"] is not None else "") + ".",
                "Run that actor less often, or narrow its roles and locations.",
                "Less spend for the same discoveries.",
                f"${actor['reported_cost']}" if actor["reported_cost"] is not None else "unknown",
                "Fewer runs may delay finding new postings."))
    if apify.get("runs", 0) >= APIFY_MIN_RUNS and apify.get("jobs_after_deduplication") is not None \
            and apify["jobs_after_deduplication"] < apify["runs"] * APIFY_JOBS_PER_RUN:
        findings.append(_finding(
            "ARCHITECTURE", "MEDIUM", "ALL", "Searches run more often than they find new jobs.",
            f"{apify['runs']} Apify runs produced {apify['jobs_after_deduplication']} jobs after deduplication "
            f"from {apify['jobs_discovered']} found.",
            "Lengthen the schedule on the saved searches that repeat the most.",
            "The same discoveries for fewer runs.", "unknown", "New postings are noticed a little later."))
    return findings


# ───────────── LLM output ─────────────

# The advisor's output contract lives with its prompt (app.prompts.contracts): one definition for the JSON Schema the
# LLM layer sends and for the validation here.
Recommendation = contracts.Recommendation
AdvisorOutput = contracts.OptimizationReport
ADVISOR_STAGES = contracts.ADVISOR_STAGES
RECOMMENDATION_CATEGORIES = contracts.RECOMMENDATION_CATEGORIES
RECOMMENDATION_PRIORITIES = contracts.RECOMMENDATION_PRIORITIES


class AdvisorOutputError(ValueError):
    pass


def validate_report(data: Any) -> AdvisorOutput:
    try:
        return AdvisorOutput.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
        raise AdvisorOutputError(f"Advisor output failed validation: {problems}") from exc


# ───────────── processing ─────────────

def rules_summary(aggregates: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    totals = aggregates.get("totals") or {}
    if not totals.get("requests"):
        return "No LLM requests were recorded in this period, so there is nothing to analyse yet."
    cost = totals.get("estimated_cost")
    high = sum(f["priority"] == "HIGH" for f in findings)
    cost_text = "unknown" if cost is None else f"${cost:,.4f}"
    unpriced = totals.get("unpriced_requests") or 0
    if unpriced:
        cost_text += f" (+{unpriced} unpriced)"
    tokens = (totals.get("input_tokens") or 0) + (totals.get("output_tokens") or 0)
    plural = "s" if len(findings) != 1 else ""
    return (f"{totals['requests']} LLM requests, {tokens:,} tokens, estimated cost {cost_text}. "
            f"{len(findings)} finding{plural} from the rule checks, {high} high priority.")


def build_report(user_id: int, start: datetime, end: datetime, aggregates: dict[str, Any],
                 findings: list[dict[str, Any]], output: AdvisorOutput | None = None,
                 provider: str | None = None, model: str | None = None, note: str | None = None) -> AIOptimizationReport:
    grouped: dict[str, list[dict[str, Any]]] = {column: [] for column in set(FINDING_GROUPS.values())}
    for finding in findings:
        grouped[FINDING_GROUPS[finding["category"]]].append({k: v for k, v in finding.items() if k != "recommendation"})
    if output is not None:
        recommendations = [r.model_dump() for r in output.recommendations]
        summary, savings, generated_by = output.summary, output.estimated_savings, "llm"
    else:
        recommendations = [f["recommendation"] for f in findings][:MAX_RECOMMENDATIONS]
        summary, savings, generated_by = rules_summary(aggregates, findings), "unknown", "rules"
    if note:
        summary = f"{summary} ({note})"
    return AIOptimizationReport(
        user_id=user_id, time_range_start=start, time_range_end=end, summary=summary, recommendations=recommendations,
        estimated_savings=(savings or "unknown")[:200], aggregates=aggregates, generated_by=generated_by,
        model_provider=provider, model_name=model,
        prompt_version=AI_OPTIMIZATION_PROMPT_VERSION if generated_by == "llm" else None, created_at=utcnow(), **grouped)


def _owned(db: Session, claim: Claim) -> AIOptimizationTask | None:
    task = db.scalar(select(AIOptimizationTask).where(AIOptimizationTask.id == claim.task_id))
    return task if task is not None and task.status == PROCESSING and task.lock_token == claim.token else None


# What the advisor's prompt loses, in this order, when it is over the AI Optimization worker's prompt token budget
# (app.services.prompt_budget): first the external aggregates the usage aggregates repeat, then detail lists, each
# from its last (least significant) row. Never trimmed: the ranges, the totals, the rule findings, per-worker routing,
# external providers and the Apify totals; stages and models keep their first ADVISOR_TRIM_KEEP rows. The report keeps
# the full aggregates; only the prompt is trimmed.
ADVISOR_TRIM_ORDER = (
    ("duplicate_external", None),
    ("outliers", ("outliers", "most_expensive")), ("outliers", ("outliers", "largest_context")),
    ("external_operations", ("external", "operations")),
    ("apify_by_saved_search", ("external", "apify_by_saved_search")),
    ("apify_by_actor", ("external", "apify_by_actor")),
    ("external_workflows", ("external", "workflows")),
    ("external_errors", ("external", "errors")),
    ("prompt_versions", ("prompt_versions",)),
    ("stage_models", ("stage_models",)),
    ("context_buckets", ("context_buckets",)),
    ("worker_models", ("routing", "worker_models")),
    ("errors", ("errors",)),
    ("models", ("models",)),
    ("stages", ("stages",)),
)
ADVISOR_TRIM_KEEP = 3


def fit_advisor_prompt(aggregates: dict[str, Any], findings: list[dict[str, Any]], budget: int | None
                       ) -> tuple[Any, dict[str, Any] | None]:
    """The advisor's prompt within `budget`, and the context stats to record with it (None without a budget).
    Without a budget the prompt is exactly build_ai_optimization_prompt(aggregates, findings, external)."""
    from app.services.context_builder import ContextStats

    if not budget:
        return build_ai_optimization_prompt(aggregates, findings, aggregates.get("external")), None
    shown = copy.deepcopy(aggregates)
    external = shown.get("external") or {}

    def lookup(path: tuple[str, ...]) -> list[Any]:
        node: Any = {**shown, "external": external}
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node if isinstance(node, list) else []

    def drop_duplicate() -> bool:  # EXTERNAL API AGGREGATES carries it already
        return shown.pop("external", None) is not None

    steps = [prompt_budget.Step(kind, drop_duplicate if path is None else prompt_budget.drop_last(
        lookup(path), ADVISOR_TRIM_KEEP if kind in ("models", "stages") else 0)) for kind, path in ADVISOR_TRIM_ORDER]
    prompt, trim = prompt_budget.fit(lambda: build_ai_optimization_prompt(shown, findings, external), steps, budget)
    stats = ContextStats.measure(trim.tokens_before, trim.tokens_after)
    stats.prompt_token_budget, stats.prompt_tokens_before_trim = budget, trim.tokens_before
    stats.prompt_tokens, stats.trimmed = trim.tokens_after, dict(trim.removed)
    return prompt, stats.as_dict()


def analyse(db: Session, user_id: int, start: datetime, end: datetime) -> AIOptimizationReport:
    """Aggregate, detect findings and (when possible) ask the model to write recommendations. Does not commit."""
    aggregates = monitoring.advisor_aggregates(db, user_id, start, end)
    # Third-party telemetry rides along in the same aggregates blob, so the report stores what it was shown.
    aggregates["external"] = external_monitoring.advisor_aggregates(db, user_id, start, end)
    findings = detect_findings(aggregates)
    if not aggregates["totals"]["requests"] and not aggregates["external"]["totals"]["requests"]:
        return build_report(user_id, start, end, aggregates, findings)
    has_settings = db.scalar(select(AppSetting.id).where(AppSetting.user_id == user_id)) is not None
    if not has_settings or not ai_ready(db, user_id)[0]:
        return build_report(user_id, start, end, aggregates, findings,
                            note="written from rule findings: no AI provider is available for this account")
    prompt, stats = fit_advisor_prompt(aggregates, [{k: v for k, v in f.items() if k != "recommendation"}
                                                    for f in findings],
                                       prompt_budget.budget_for(db, user_id, "AI_OPTIMIZATION"))
    try:
        result = LLMClient.for_user(db, user_id).generate_prompt(
            prompt, LLMCall(workflow_type="system", validate=validate_report, context_stats=stats))
    except Exception as exc:  # the rule findings still make a useful report
        log.info("advisor LLM call failed for user %s: %s", user_id, exc)
        return build_report(user_id, start, end, aggregates, findings,
                            note=f"written from rule findings: the AI write-up failed ({type(exc).__name__})")
    return build_report(user_id, start, end, aggregates, findings, output=result.validated,
                        provider=result.provider, model=result.model)


def process(sessions: SessionFactory, claim: Claim) -> str:
    """Run one claimed analysis in a session bound to its owner. Returns completed, retrying, failed or lost."""
    with sessions() as db:
        db.info["user_id"] = claim.user_id
        task = _owned(db, claim)
        if task is None:
            return "lost"
        start, end = task.time_range_start, task.time_range_end
        try:
            report = analyse(db, claim.user_id, start, end)
            db.add(report)
            db.flush()
            task = _owned(db, claim)
            if task is None:
                db.rollback()
                return "lost"
            task.status, task.report_id, task.completed_at, task.error = COMPLETED, report.id, utcnow(), None
            task.locked_by = task.lock_token = task.lock_expires_at = None
            db.commit()
            return "completed"
        except Exception as exc:
            db.rollback()
            log.exception("AI optimization task %s failed", claim.task_id)
            task = _owned(db, claim)
            if task is None:
                return "lost"
            task.locked_by = task.lock_token = task.lock_expires_at = None
            task.error = f"{type(exc).__name__}: {exc}"[:2000]
            if task.attempt_count < MAX_ATTEMPTS:
                task.status, task.next_attempt_at = PENDING, utcnow() + timedelta(minutes=2 ** task.attempt_count)
                db.commit()
                return "retrying"
            task.status, task.completed_at = FAILED, utcnow()
            db.commit()
            return "failed"


# ───────────── read models ─────────────

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def task_dict(task: AIOptimizationTask) -> dict[str, Any]:
    return {"id": task.id, "status": task.status, "trigger": task.trigger, "attempt_count": task.attempt_count,
            "time_range_start": _iso(task.time_range_start), "time_range_end": _iso(task.time_range_end),
            "created_at": _iso(task.created_at), "started_at": _iso(task.started_at),
            "completed_at": _iso(task.completed_at), "error": task.error, "report_id": task.report_id}


def report_dict(report: AIOptimizationReport, include_aggregates: bool = False) -> dict[str, Any]:
    fields = ("id", "summary", "recommendations", "estimated_savings", "cost_findings", "latency_findings",
              "context_findings", "model_findings", "prompt_findings", "reliability_findings", "generated_by",
              "model_provider", "model_name", "prompt_version")
    out = {**{name: getattr(report, name) for name in fields}, "time_range_start": _iso(report.time_range_start),
           "time_range_end": _iso(report.time_range_end), "created_at": _iso(report.created_at)}
    if include_aggregates:
        out["aggregates"] = report.aggregates
    return out
