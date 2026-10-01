"""The application tracker's stages, and the one way an application's stage is changed.

A person moving a card (app.workspace) and the Application Inbox applying an email's stage change
(app.services.application_inbox) both call `set_stage`, so the timeline records every move the same way.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent

BASE_STAGES = [
    {"id": "applied", "label": "Applied", "dot": "#5b4ae8"},
    {"id": "acknowledged", "label": "Acknowledged", "dot": "#a79bf0"},
    {"id": "screening", "label": "Screening", "dot": "#0f9c86"},
    {"id": "interviewing", "label": "Interviewing", "dot": "#0e7a68"},
]
EXTRA_STAGES = [
    {"id": "takehome", "label": "Take-home", "dot": "#e0a136"},
    {"id": "offer", "label": "Offer", "dot": "#0f9c86"},
    {"id": "negotiating", "label": "Negotiating", "dot": "#b26b00"},
    {"id": "onhold", "label": "On hold", "dot": "#9aa1b2"},
    {"id": "rejected", "label": "Rejected", "dot": "#c22f4e"},
    {"id": "referral", "label": "Referral outreach", "dot": "#a79bf0"},
]
CLOSED_STAGE = {"id": "closed", "label": "Closed", "dot": "#c6cad8"}
ALL_STAGE_IDS = {s["id"] for s in BASE_STAGES + EXTRA_STAGES} | {"closed"}
REPLY_STAGES = {"screening", "interviewing", "takehome", "offer", "negotiating"}
INTERVIEW_STAGES = {"interviewing", "takehome", "offer", "negotiating"}
INACTIVE_STAGES = {"closed", "rejected"}
EXTRA_STAGE_IDS = {s["id"] for s in EXTRA_STAGES}


def stage_label(stage: str) -> str:
    return next((s["label"] for s in [*BASE_STAGES, *EXTRA_STAGES, CLOSED_STAGE] if s["id"] == stage), stage.title())


def enable_extra_stage(db: Session, app: Application, stage: str) -> bool:
    """Show an optional column (Offer, Rejected, …) on the board, so an application moved there stays visible."""
    if stage not in EXTRA_STAGE_IDS:
        return False
    from app.services.user_settings import settings_for

    row = settings_for(db, app.user_id)
    prefs = dict(row.preferences or {})
    extras = list(prefs.get("extra_stages") or [])
    if stage in extras:
        return False
    prefs["extra_stages"] = [*extras, stage]
    row.preferences = prefs
    return True


def set_stage(db: Session, app: Application, stage: str, *, meta: dict[str, Any] | None = None,
              title: str | None = None) -> ApplicationEvent | None:
    """Move an application to `stage` and record the move on its timeline. Flushes, does not commit.

    Returns the timeline event, or None when the application is already there."""
    if stage not in ALL_STAGE_IDS:
        raise ValueError(f"Unknown stage {stage}")
    if (app.stage or "applied") == stage:
        return None
    event = ApplicationEvent(application=app, kind="stage", title=title or f"Moved to {stage_label(stage)}",
                             detail=stage, meta=meta or None)
    db.add(event)
    app.stage = stage
    db.flush()
    return event
