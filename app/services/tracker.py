"""The application tracker's stages, and the one way an application's stage is changed.

A person moving a card (app.workspace) and the Application Inbox applying an email's stage change
(app.services.application_inbox) both call `set_stage`, so the timeline records every move the same way.

Archiving (`archive_older_than`) takes applications off the board without losing them: `archived_at` is set, a live
one is also moved to Closed (so it gets no follow-up and no Gmail check), a Rejected or Closed one keeps its stage.
Moving an archived card to another stage, or `unarchive`, puts it back on the board.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
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
# What "archive old applications" may sweep: the ones still waiting on the employer, every stage that is still live
# (Offer and Negotiating included: the preview names them before anything moves), the finished ones (Rejected,
# Closed), or all of them.
LIVE_STAGE_IDS = tuple(s["id"] for s in BASE_STAGES + EXTRA_STAGES if s["id"] not in INACTIVE_STAGES)
FINISHED_STAGE_IDS = ("rejected", "closed")
ARCHIVE_SCOPES = {"no_reply": ("applied", "acknowledged"), "live": LIVE_STAGE_IDS, "finished": FINISHED_STAGE_IDS,
                  "all": (*LIVE_STAGE_IDS, *FINISHED_STAGE_IDS)}


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
    app.archived_at = None  # a card moved is back in play (archiving sets it again after its own move)
    db.flush()
    return event


@dataclass
class ArchiveSweep:
    days: int
    scope: str
    cutoff: datetime
    applications: list[Application] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts = Counter(app.stage or "applied" for app in self.applications)
        return {"days": self.days, "scope": self.scope, "cutoff": self.cutoff.isoformat(),
                "count": len(self.applications),
                "by_stage": [{"stage": stage, "label": stage_label(stage), "count": counts[stage]}
                             for stage in ARCHIVE_SCOPES[self.scope] if counts[stage]]}


def older_than(db: Session, days: int, scope: str = "no_reply", *, now: datetime | None = None) -> ArchiveSweep:
    """The submitted, not yet archived applications of the session's account sent more than `days` ago (0: any
    age) and in one of the scope's stages. Reads only."""
    if scope not in ARCHIVE_SCOPES:
        raise ValueError(f"Unknown scope {scope}")
    if days < 0:
        raise ValueError("Pick a period of zero days or more")
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)
    stages = ARCHIVE_SCOPES[scope]
    apps = db.scalars(select(Application).where(Application.applied_at.is_not(None), Application.applied_at <= cutoff,
                                                Application.archived_at.is_(None))
                      .order_by(Application.applied_at)).all()
    return ArchiveSweep(days, scope, cutoff, [a for a in apps if (a.stage or "applied") in stages])


def archive_older_than(db: Session, days: int, scope: str = "no_reply", *,
                       now: datetime | None = None) -> dict[str, Any]:
    """Archive every application `older_than` finds, each recorded on its own timeline, and return what was archived
    (its summary). Does not commit. A live one moves to Closed: a closed application gets no follow-up and no Gmail
    check (application_inbox.queries); moving it back restores both."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    sweep = older_than(db, days, scope, now=now)
    summary = sweep.summary()
    title = f"Archived: submitted more than {days} days ago" if days else "Archived"
    for app in sweep.applications:
        if (app.stage or "applied") in FINISHED_STAGE_IDS:
            db.add(ApplicationEvent(application=app, kind="archive", title=title, detail=app.stage,
                                    meta={"archived_after_days": days}))
        else:
            set_stage(db, app, "closed", meta={"archived_after_days": days}, title=title)
        app.archived_at = now
    db.flush()
    return summary


def unarchive(db: Session, app: Application) -> bool:
    """Put an archived application back on the board, in the stage it has. Does not commit. False when it was not
    archived."""
    if app.archived_at is None:
        return False
    app.archived_at = None
    db.add(ApplicationEvent(application=app, kind="archive", title="Back on the board", detail=app.stage or "applied"))
    db.flush()
    return True
