"""Pick the model a request goes to when the worker's primary is resting.

    model_resolution.get → ResolvedWorker (the active configuration: primary, fallback, secondary models)
    failover.apply       → the same worker with the chain re-ordered by route health, or unchanged

A worker keeps its configured primary while that route is healthy. When it is resting (a 429, or failures beyond the
account's thresholds, see app.services.model_health) the request goes to the best healthy model in the chain, chosen
by the account's routing algorithm; resting models stay at the end as a last resort, so a request is never left with
nothing to call. This is an overlay: no configuration version is written. Quality escalation is a different thing and
never goes through here. Fails open: any error here leaves the configured worker untouched.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Any

from app.services import failover_settings, model_health, token_pacing
from app.services.model_resolution import ResolvedWorker

log = logging.getLogger("jobhunt.failover")

_lock = threading.Lock()
_settings: dict[int, tuple[float, dict[str, Any]]] = {}
SETTINGS_CACHE_SECONDS = 5.0


def _config(user_id: int) -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        hit = _settings.get(user_id)
        if hit and hit[0] > now:
            return hit[1]
    with model_health._session_factory()() as db:
        cfg = failover_settings.for_user(db, user_id)
    with _lock:
        _settings[user_id] = (now + SETTINGS_CACHE_SECONDS, cfg)
    return cfg


def reset_cache() -> None:
    with _lock:
        _settings.clear()


def _key(algorithm: str, user_id: int, worker_type: str, model: str, position: int) -> tuple[float, int]:
    """Sort key among healthy candidates; lower is better, configured order breaks ties."""
    score, stats = model_health.route_score(user_id, model, worker_type)
    if algorithm == "LATENCY":
        return (stats.get("p95_ms") if stats.get("p95_ms") is not None else float("inf"), position)
    if algorithm == "LEAST_ERRORS":
        return (float(stats.get("failure_pct") or 0.0), position)
    if algorithm == "WEIGHTED":
        return (-(score if score is not None else 50.0), position)
    return (0.0, position)  # PRIORITY: the configured order


def order(worker: ResolvedWorker, user_id: int, cfg: dict[str, Any], tokens_needed: int | None = None) -> list[str]:
    chain = [worker.model, *worker.chain()]
    healthy = [m for m in chain if not model_health.resting(user_id, m, worker.worker_type)
               and not _waiting(user_id, m, tokens_needed)]
    if worker.model in healthy:  # the primary keeps its place; a resting backup moves behind the healthy ones
        return [*healthy, *(m for m in chain if m not in healthy)]
    rest = [m for m in chain if m not in healthy]
    ranked = sorted(healthy, key=lambda m: _key(cfg["algorithm"], user_id, worker.worker_type, m, chain.index(m)))
    return [*ranked, *rest]


def _waiting(user_id: int, model: str, tokens_needed: int | None) -> bool:
    """Whether the model's per-minute token window cannot take this request until it resets (token_pacing)."""
    return bool(tokens_needed) and token_pacing.waiting(user_id, f"LITELLM:{model}", tokens_needed) is not None


def apply(worker: ResolvedWorker, user_id: int | None, *, tokens_needed: int | None = None) -> ResolvedWorker:
    """The worker with its models ordered by health. The same object when nothing is resting.

    `tokens_needed` (a gateway request's estimated prompt plus answer) also moves a model whose token window is
    exhausted for this minute behind the others, until the window resets."""
    try:
        if user_id is None or not worker.model or not worker.chain():
            return worker
        if not model_health.states(user_id) and not _waiting(user_id, worker.model, tokens_needed):
            return worker
        cfg = _config(user_id)
        if not cfg["enabled"]:
            return worker
        ordered = order(worker, user_id, cfg, tokens_needed)
        if ordered == [worker.model, *worker.chain()]:
            return worker
        if ordered[0] == worker.model:  # only the backups were reordered: no failover, nothing to note
            return replace(worker, fallback_model=ordered[1] if len(ordered) > 1 else None,
                           secondary_models=tuple(ordered[2:]), model_fallbacks=(),
                           capabilities={**worker.capabilities})
        why = ("is waiting for its token window" if _waiting(user_id, worker.model, tokens_needed)
               and not model_health.resting(user_id, worker.model, worker.worker_type) else "is resting")
        note = f"failover: {worker.model} {why}; using {ordered[0]}"
        return replace(worker, model=ordered[0], fallback_model=ordered[1] if len(ordered) > 1 else None,
                       secondary_models=tuple(ordered[2:]), model_fallbacks=(), failed_over_from=worker.model,
                       note="; ".join(x for x in (worker.note, note) if x),
                       capabilities={**worker.capabilities})
    except Exception:
        log.warning("failover could not order the models; using the configured ones", exc_info=True)
        return worker
