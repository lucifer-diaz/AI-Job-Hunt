"""Import what LiteLLM knows about a request that Job Hunt OS could not see in the response: the auto router's
decision (tier and cause), from LiteLLM's own spend log for that request.

The response headers already give the model group and deployment that answered, so this only fills
`routing_tier` / `routing_cause` on auto-router requests. Nothing is duplicated: tokens, latency and cost stay as
Job Hunt OS measured them. The LiteLLM call id (x-litellm-call-id) is the join key; the Job Hunt request id, the
external API request id and the trace id are already on the same row, so all four identifiers correlate.

Best effort and bounded: a few dozen rows per account per run, only recent requests, and a request LiteLLM has no
log for (spend logs off, or not written yet after an hour) is marked `unavailable` instead of retried forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.models import LLMRequestLog
from app.services import ai_model_config
from app.services.litellm_client import LiteLLMAPI, LiteLLMError

BATCH = 40
WINDOW = timedelta(days=2)
GIVE_UP_AFTER = timedelta(hours=1)
UNAVAILABLE = "unavailable"


def routing_decision(row: dict[str, Any] | None) -> dict[str, Any] | None:
    metadata = (row or {}).get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    decision = metadata.get("routing_decision") if isinstance(metadata, dict) else None
    return decision if isinstance(decision, dict) else None


def import_routing(sessions: Any, now: datetime) -> dict[str, int]:
    updated = unavailable = accounts = 0
    with sessions() as db:
        pending = db.execute(
            select(LLMRequestLog.id, LLMRequestLog.user_id, LLMRequestLog.litellm_call_id, LLMRequestLog.created_at)
            .where(LLMRequestLog.litellm_call_id.is_not(None), LLMRequestLog.routing_cause.is_(None),
                   LLMRequestLog.model_mode == "AUTO_ROUTER", LLMRequestLog.configured_model == "AUTO_ROUTER",
                   LLMRequestLog.user_id.is_not(None), LLMRequestLog.created_at >= now - WINDOW)
            .order_by(LLMRequestLog.created_at.desc()).limit(BATCH * 20)).all()
        by_user: dict[int, list[Any]] = {}
        for row in pending:
            if len(by_user.setdefault(row.user_id, [])) < BATCH:
                by_user[row.user_id].append(row)
        for user_id, rows in by_user.items():
            url, key = ai_model_config.gateway_settings(db, user_id)
            db.commit()
            if not url:
                continue
            accounts += 1
            results: dict[int, tuple[str | None, str]] = {}
            try:
                with LiteLLMAPI(url, key, user_id=user_id, service="telemetry_import") as api:
                    for row in rows:
                        decision = routing_decision(api.spend_log(row.litellm_call_id))
                        if decision:
                            results[row.id] = (decision.get("tier"), str(decision.get("cause") or "routed")[:60])
                        elif now - row.created_at > GIVE_UP_AFTER:
                            results[row.id] = (None, UNAVAILABLE)
            except LiteLLMError:
                continue  # LiteLLM is down: try again next run
            for log in db.scalars(select(LLMRequestLog).where(LLMRequestLog.id.in_(list(results)))).all():
                tier, cause = results[log.id]
                log.routing_tier = log.routing_tier or (str(tier)[:30] if tier else None)
                log.routing_cause = cause
                updated += cause != UNAVAILABLE
                unavailable += cause == UNAVAILABLE
            db.commit()
    return {"accounts": accounts, "updated": updated, "unavailable": unavailable}
