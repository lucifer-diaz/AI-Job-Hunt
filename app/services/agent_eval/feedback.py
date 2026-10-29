"""Human correction rates and the recruiter's outcomes, from stored facts, kept apart.

Model quality here is what a person corrected in an automated result:

    inbox       a classification (RULE or LLM) a person corrected      application_communication_events.user_corrected
    review      an approval that overrode the reviewer's minimum       applications.approval_override
    jd_parser   a parsed posting a person edited                       job_structured_data_changes (change_source USER)

The recruiter's decision (rejected, interviewing, offer) is reported beside it and never folded into it: a rejection
says nothing about which stage, model or packet caused it, so nothing here infers one. Read-only, per account,
deterministic; no model is called.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationCommunicationEvent,
    JobDescriptionParseTask,
    JobStructuredDataChange,
)

OUTCOME_GROUPS = {"interview": ("screening", "interviewing", "takehome"), "offer": ("offer", "negotiating"),
                  "rejected": ("rejected",), "closed": ("closed",)}
LIMIT = 5000


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def rates(facts: dict[str, Any]) -> dict[str, Any]:
    """Correction rates from plain facts (see `collect`). Unknown (no samples) is None, never 0."""
    inbox = [e for e in facts.get("inbox_events") or [] if e.get("method") != "USER"]
    approvals = facts.get("approvals") or []
    parsed = int(facts.get("jd_parsed_jobs") or 0)
    edited = len(set(facts.get("jd_corrected_jobs") or []))
    signals = {
        "inbox": {"corrected": sum(bool(e.get("user_corrected")) for e in inbox), "samples": len(inbox)},
        "review": {"corrected": sum(bool(a.get("override")) for a in approvals), "samples": len(approvals)},
        "jd_parser": {"corrected": min(edited, parsed), "samples": parsed},
    }
    for s in signals.values():
        s["rate"] = _rate(s["corrected"], s["samples"])
    corrected = sum(s["corrected"] for s in signals.values())
    samples = sum(s["samples"] for s in signals.values())
    stages = Counter(a.get("stage") for a in facts.get("applications") or [] if a.get("applied"))
    outcome = {group: sum(stages.get(s, 0) for s in members) for group, members in OUTCOME_GROUPS.items()}
    return {"human_correction_rate": _rate(corrected, samples), "corrected": corrected, "samples": samples,
            "by_signal": signals,
            "recruiter_outcome": {"submitted": sum(stages.values()), **outcome, "attributed_to_model": False,
                                  "note": "The recruiter's decision. Never attributed to a model, a packet or a "
                                          "stage, and never counted as a correction."}}


def collect(db: Session, user_id: int) -> dict[str, Any]:
    """The account's own facts for `rates`. Bounded; ids, flags and stages only."""
    inbox = db.execute(select(ApplicationCommunicationEvent.classification_method,
                              ApplicationCommunicationEvent.user_corrected)
                       .where(ApplicationCommunicationEvent.user_id == user_id)
                       .order_by(ApplicationCommunicationEvent.id.desc()).limit(LIMIT)).all()
    apps = db.execute(select(Application.approved_at, Application.approval_override, Application.stage,
                             Application.applied_at)
                      .where(Application.user_id == user_id).order_by(Application.id.desc()).limit(LIMIT)).all()
    parsed = db.scalar(select(func.count(func.distinct(JobDescriptionParseTask.job_id)))
                       .where(JobDescriptionParseTask.user_id == user_id,
                              JobDescriptionParseTask.status == "COMPLETED")) or 0
    corrected = db.scalars(select(func.distinct(JobStructuredDataChange.job_id))
                           .where(JobStructuredDataChange.user_id == user_id,
                                  JobStructuredDataChange.change_source == "USER").limit(LIMIT)).all()
    return {"inbox_events": [{"method": r[0], "user_corrected": bool(r[1])} for r in inbox],
            "approvals": [{"override": bool(r[1])} for r in apps if r[0] is not None],
            "jd_parsed_jobs": parsed, "jd_corrected_jobs": list(corrected),
            "applications": [{"stage": r[2], "applied": r[3] is not None} for r in apps]}


def report(db: Session, user_id: int) -> dict[str, Any]:
    return rates(collect(db, user_id))
