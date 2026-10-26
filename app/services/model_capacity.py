"""Requests too big for their model's per-minute limit, and what would make them fit.

A provider such as Groq refuses a single request that is larger than a per-minute token limit ("Request too large …
(TPM): Limit 8000, Requested 9197"). Resting the model does not help that request (app.services.rate_limits), so
this module shows, per worker and model, what was refused and the options that fit:

* a cap on the worker's output tokens, when the limit counts output and the worker's own answers fit under it;
* other models with room for the worker's requests: no refusal of a request that size, and either a known context
  window large enough or proof (they have already answered this worker's requests of that size; proven come first).
  A model with an unknown window and no such answer is never offered: unknown stays unknown;
* plainly, when the prompt alone is over the limit: only another model helps.

Read-only and deterministic: everything comes from the account's own `llm_request_logs` rows (bounded, filtered by
user_id explicitly, like every telemetry read) and the draft configuration. It recommends; a person applies a fix
through the normal draft → apply path. Never raises.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services import model_resolution, rate_limits
from app.services.ai_workers import label

log = logging.getLogger("jobhunt.model_capacity")

WINDOW_DAYS = 7
MAX_REFUSALS = 500
MAX_SUCCESSES = 3000
MAX_MODELS = 4
INPUT_MARGIN = 1.1  # our token estimate is characters / 4; the provider's tokenizer may count more
OUTPUT_MARGIN = 1.1  # room above the worker's typical answer
MIN_OUTPUT_CAP = 256
STEP = 50


def _p90(values: list[int]) -> int | None:
    values = sorted(v for v in values if v is not None)
    return values[min(len(values) - 1, max(0, math.ceil(0.9 * len(values)) - 1))] if values else None


def _roles(snapshot: dict[str, Any], worker_type: str) -> tuple[dict[str, str], list[str]]:
    """Each model a worker sends to with its role there (primary, backup, escalation), and its backups in order.
    A fix replaces the model in the role it has; the primary wins when a model has more than one."""
    config = (snapshot.get("workers") or {}).get(worker_type) or {}
    primary = model_resolution.resolve_model(snapshot, worker_type)[0]
    chain = [m for m in [config.get("fallback_model"), *(config.get("secondary_models") or [])] if m]
    escalation = config.get("escalation") or {}
    target = escalation.get("model") or (model_resolution.tier_model(snapshot, escalation["tier"])[0]
                                         if escalation.get("tier") else None)
    roles = {target: "escalation"} if target else {}
    roles.update(dict.fromkeys(chain, "backup"))
    if primary:
        roles[primary] = "primary"
    return roles, chain


def report(db: Session, user_id: int, snapshot: dict[str, Any], *, now: Any = None) -> list[dict[str, Any]]:
    try:
        return _report(db, user_id, snapshot, now=now)
    except Exception:  # noqa: BLE001  a suggestion never breaks the screen
        log.warning("could not work out request-size suggestions", exc_info=True)
        return []


def _report(db: Session, user_id: int, snapshot: dict[str, Any], *, now: Any = None) -> list[dict[str, Any]]:
    from app.models import LLMRequestLog
    from app.services.model_health import utcnow

    since = (now or utcnow()) - timedelta(days=WINDOW_DAYS)
    refused = db.execute(
        select(LLMRequestLog.worker_type, LLMRequestLog.requested_model, LLMRequestLog.estimated_context_tokens,
               LLMRequestLog.error_message, LLMRequestLog.created_at)
        .where(LLMRequestLog.user_id == user_id, LLMRequestLog.status == "FAILED",
               LLMRequestLog.error_type == "RATE_LIMIT", LLMRequestLog.created_at >= since,
               LLMRequestLog.error_message.ilike("%too large%"))
        .order_by(LLMRequestLog.created_at.desc()).limit(MAX_REFUSALS)).all()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    smallest_refused: dict[str, int] = {}  # model → the smallest total request it refused (any worker)
    for worker_type, model, estimate, message, at in refused:
        limit = rate_limits.size_limit(message)
        if not worker_type or not model or limit is None:
            continue
        group = groups.setdefault((worker_type, model), {"count": 0, "last": at, "inputs": [], "limits": {}})
        group["count"] += 1
        group["inputs"].append(estimate)
        group["limits"].setdefault((limit.deployment, limit.kind), {"limit": limit.limit, "requested": 0})
        entry = group["limits"][(limit.deployment, limit.kind)]
        entry["requested"] = max(entry["requested"], limit.requested)
        if limit.counts == "total":
            smallest_refused[model] = min(smallest_refused.get(model, limit.requested), limit.requested)
    if not groups:
        return []

    workers = {w for w, _ in groups}
    succeeded = db.execute(
        select(LLMRequestLog.worker_type, LLMRequestLog.requested_model, LLMRequestLog.model_group,
               LLMRequestLog.fallback_used, LLMRequestLog.input_tokens, LLMRequestLog.output_tokens)
        .where(LLMRequestLog.user_id == user_id, LLMRequestLog.status == "SUCCEEDED",
               LLMRequestLog.created_at >= since, LLMRequestLog.worker_type.in_(workers))
        .order_by(LLMRequestLog.created_at.desc()).limit(MAX_SUCCESSES)).all()
    outputs: dict[str, list[int]] = defaultdict(list)
    largest_answered: dict[tuple[str, str], int] = {}
    for worker_type, requested, group, fell_back, tokens_in, tokens_out in succeeded:
        if tokens_out is not None:
            outputs[worker_type].append(tokens_out)
        # Credit the catalog model that answered: LiteLLM's model group, or the requested model when nothing fell
        # back. A fallback answer without a group names no catalog model, so it proves nothing.
        model = group or (requested if not fell_back else None)
        if model and tokens_in is not None:
            key = (worker_type, model)
            largest_answered[key] = max(largest_answered.get(key, 0), tokens_in + (tokens_out or 0))

    catalog = snapshot.get("models") or {}
    out = []
    for (worker_type, model), group in groups.items():
        roles, chain = _roles(snapshot, worker_type)
        if model not in roles:
            continue  # the worker no longer sends to this model: nothing to fix
        config = (snapshot.get("workers") or {}).get(worker_type) or {}
        typical_input = _p90(group["inputs"])
        typical_output = _p90(outputs.get(worker_type, []))
        limits = [{"deployment": d, "kind": k, "counts": rate_limits.COUNTS.get(k, "total"), **v}
                  for (d, k), v in sorted(group["limits"].items())]
        caps, input_over = [], False
        for item in limits:
            if item["counts"] == "output":
                caps.append(item["limit"])
            elif typical_input is not None:
                prompt = math.ceil(typical_input * INPUT_MARGIN)
                if prompt >= item["limit"]:
                    input_over = True  # the prompt alone does not fit: no output cap helps
                elif item["counts"] == "total":
                    caps.append(item["limit"] - prompt)
        cap = None
        if caps and not input_over:
            value = min(caps) // STEP * STEP
            enough = None if typical_output is None else value >= typical_output * OUTPUT_MARGIN
            if value >= MIN_OUTPUT_CAP and enough is not False:
                current = config.get("max_output_tokens")
                cap = {"value": value, "typical_output": typical_output, "known_safe": enough is True,
                       "applied": bool(current and current <= value), "current": current}
        need = (typical_input or 0) + (typical_output or 0)
        models = []
        for name, entry in catalog.items():
            if name == model or not entry.get("enabled", True) or entry.get("model_type") in ("EMBEDDING",):
                continue
            window = entry.get("context_window")
            if smallest_refused.get(name, need + 1) <= need:
                continue
            proven = largest_answered.get((worker_type, name), 0) >= need > 0
            if not proven and not (window and window >= need * INPUT_MARGIN):
                continue  # no evidence it has room: an unknown window is not room
            models.append({"name": name, "context_window": window, "proven": proven, "in_chain": name in chain})
        models.sort(key=lambda m: (not m["proven"], m["context_window"] is None, -(m["context_window"] or 0),
                                   m["name"]))
        out.append({"worker_type": worker_type, "label": label(worker_type), "model": model, "role": roles[model],
                    "count": group["count"], "last_at": group["last"].isoformat() if group["last"] else None,
                    "limits": limits, "typical_input": typical_input, "typical_output": typical_output,
                    "input_over": input_over, "output_cap": cap, "models": models[:MAX_MODELS],
                    "has_backup": bool(chain)})
    out.sort(key=lambda g: -g["count"])
    return out
