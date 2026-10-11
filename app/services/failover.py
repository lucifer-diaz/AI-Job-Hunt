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

from app.services import failover_settings, model_health
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


def order(worker: ResolvedWorker, user_id: int, cfg: dict[str, Any]) -> list[str]:
    chain = [worker.model, *worker.chain()]
    healthy = [m for m in chain if not model_health.resting(user_id, m, worker.worker_type)]
    if worker.model in healthy:
        return chain  # the primary is healthy: nothing changes
    rest = [m for m in chain if m not in healthy]
    ranked = sorted(healthy, key=lambda m: _key(cfg["algorithm"], user_id, worker.worker_type, m, chain.index(m)))
    return [*ranked, *rest]


def apply(worker: ResolvedWorker, user_id: int | None) -> ResolvedWorker:
    """The worker with its models ordered by health. The same object when nothing is resting."""
    try:
        if user_id is None or not worker.model or not worker.chain() or not model_health.states(user_id):
            return worker
        cfg = _config(user_id)
        if not cfg["enabled"]:
            return worker
        ordered = order(worker, user_id, cfg)
        if ordered[0] == worker.model:
            return worker
        note = f"failover: {worker.model} is resting; using {ordered[0]}"
        return replace(worker, model=ordered[0], fallback_model=ordered[1] if len(ordered) > 1 else None,
                       secondary_models=tuple(ordered[2:]), model_fallbacks=(), failed_over_from=worker.model,
                       note="; ".join(x for x in (worker.note, note) if x),
                       capabilities={**worker.capabilities})
    except Exception:
        log.warning("failover could not order the models; using the configured ones", exc_info=True)
        return worker
