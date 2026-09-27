"""A custom auto-router classifier for LiteLLM that routes on Job Hunt OS request metadata.

This file runs inside the LiteLLM proxy, not inside Job Hunt OS: it imports nothing from the app. Job Hunt OS sends
metadata with every gateway request (app.services.model_resolution.request_metadata) — the worker type, the
retry number, whether structured output is required, the confidence a worker needs and, on an escalated request,
the previous answer's confidence. This classifier turns that into one of the complexity router's four tiers.

Use it (LiteLLM 1.102+, complexity router `classifier_type: custom`):

  1. Put this file on the LiteLLM proxy's PYTHONPATH (for example next to its config.yaml).
  2. In Settings → AI Models → Routing choose the "Custom Job Hunt Classifier" and set the plugin path to
     `jobhunt_classifier.router`.
  3. Apply. LiteLLM validates the router config before Job Hunt OS deploys it.

The mapping below is a starting point, not a requirement: edit WORKER_TIERS to taste. Returning None declines,
and the router's `classifier_fallback` (the heuristic scorer by default) decides instead.
"""
from __future__ import annotations

from typing import Any

WORKER_TIERS = {
    "INBOX_CLASSIFIER": "SIMPLE", "JOB_NORMALIZATION": "SIMPLE", "FOLLOWUP": "MEDIUM",
    "COMPANY_INSIGHTS": "MEDIUM", "INTERVIEW_INSIGHTS": "MEDIUM", "APPLICATION_ANSWER": "MEDIUM",
    "RESUME_PARSER": "COMPLEX", "JD_PARSER": "COMPLEX", "RANKER": "COMPLEX", "RESUME_MATCHER": "COMPLEX",
    "TAILOR": "COMPLEX", "EVIDENCE_EXTRACTION": "COMPLEX", "REVIEWER": "REASONING", "AI_OPTIMIZATION": "REASONING",
}
ORDER = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")


def _metadata(context: Any) -> dict[str, Any]:
    metadata = getattr(context, "metadata", None) or {}
    # The proxy nests caller metadata differently across versions; look in the usual places.
    for key in ("requester_metadata", "metadata"):
        nested = metadata.get(key) if isinstance(metadata, dict) else None
        if isinstance(nested, dict) and nested.get("jobhunt"):
            return nested
    return metadata if isinstance(metadata, dict) else {}


def _bump(tier: str, steps: int = 1) -> str:
    return ORDER[min(len(ORDER) - 1, ORDER.index(tier) + steps)]


def tier_for(metadata: dict[str, Any]) -> str | None:
    """The tier for one request's Job Hunt OS metadata, or None to let the fallback classifier decide."""
    if not metadata.get("jobhunt"):
        return None
    tier = WORKER_TIERS.get(str(metadata.get("worker_type") or ""))
    if tier is None:
        return None
    if metadata.get("escalation"):  # Job Hunt OS already judged the first answer not good enough
        tier = _bump(tier)
    if int(metadata.get("retry_number") or 0) >= 2:
        tier = _bump(tier)
    previous = metadata.get("previous_confidence")
    required = metadata.get("confidence_required")
    if isinstance(previous, (int, float)) and isinstance(required, (int, float)) and previous < required:
        tier = _bump(tier)
    if metadata.get("structured_output_required") and tier == "SIMPLE" and metadata.get("retry_number"):
        tier = "MEDIUM"  # a small model already failed a typed answer once
    return tier


class JobHuntClassifier:
    """LiteLLM ClassifierPlugin: `async def classify(context) -> str | None`."""

    async def classify(self, context: Any) -> str | None:
        return tier_for(_metadata(context))


router = JobHuntClassifier()
