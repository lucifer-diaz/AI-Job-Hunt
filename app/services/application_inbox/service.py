"""What the API, the tracker and the Follow-Up worker ask of the Application Inbox.

Corrections are the person's decisions: they are recorded field by field on the event, applied at once, and never
overwritten by a later sync or analysis (app.services.application_inbox.analysis.save_event honours them).
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.models import (
    EMAIL_EVENT_TYPES,
    Application,
    ApplicationCommunicationEvent,
    ApplicationEmailMessage,
    ApplicationEvent,
    ApplicationInboxDigest,
    GmailConnection,
)
from app.services import gmail, tracker
from app.services.application_inbox import analysis, classify, digest
from app.settings import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── follow-up integration ─────────────

def reply_received(db: Session, app: Application) -> ApplicationCommunicationEvent | None:
    """The latest reply from the employer since the application was submitted, if the inbox saw one."""
    return db.scalar(select(ApplicationCommunicationEvent).where(
        ApplicationCommunicationEvent.application_id == app.id,
        ApplicationCommunicationEvent.event_type.in_(classify.HUMAN_REPLY_TYPES),
        ApplicationCommunicationEvent.review_status != "DISMISSED",
        (ApplicationCommunicationEvent.received_at >= app.applied_at) if app.applied_at else true())
        .order_by(ApplicationCommunicationEvent.received_at.desc()).limit(1))


def blocks_followup(db: Session, app: Application) -> bool:
    """Whether a follow-up would be unnecessary: the employer already replied, or the application moved on."""
    if (app.stage or "applied") not in ("applied", "acknowledged"):
        return True
    return reply_received(db, app) is not None


# ───────────── connection status ─────────────

def status(db: Session) -> dict[str, Any]:
    settings = get_settings()
    conn = db.scalar(select(GmailConnection).limit(1))
    latest = digest.latest(db)
    return {
        "configured": settings.gmail_oauth_configured, "enabled": settings.application_inbox_enabled,
        "interval_hours": settings.application_inbox_interval_hours,
        "scope": gmail.GMAIL_READONLY_SCOPE,
        "connection": None if conn is None else {
            "status": conn.status, "email": conn.email_address, "connected_at": _iso(conn.connected_at),
            "last_sync_at": _iso(conn.last_sync_at), "last_successful_sync_at": _iso(conn.last_successful_sync_at),
            "last_error": conn.last_error, "syncing": bool(conn.sync_locked_by),
            # The running sync's phase and percent; after it ends, how the last one finished.
            "progress": conn.sync_progress},
        "thresholds": {"auto_match": settings.application_email_auto_match_threshold,
                       "review": settings.application_email_review_threshold,
                       "stage_auto_update": settings.application_stage_auto_update_threshold},
        "last_run": (latest.sync_runs or [None])[-1] if latest else None,
        **digest.pending_counts(db),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ───────────── events ─────────────

def events_for_application(db: Session, application_id: int) -> list[dict[str, Any]]:
    app = db.get(Application, application_id)
    if app is None:
        return []
    conn = db.scalar(select(GmailConnection).limit(1))
    rows = db.scalars(select(ApplicationCommunicationEvent)
                      .where(ApplicationCommunicationEvent.application_id == application_id)
                      .order_by(ApplicationCommunicationEvent.received_at.desc())).all()
    return [{**digest.event_card(e, {app.id: app}),
             "gmail_url": gmail.gmail_link(conn.email_address if conn else None, e.gmail_thread_id)} for e in rows]


def _record(event: ApplicationCommunicationEvent, field: str, old: Any, new: Any) -> None:
    event.corrections = [*(event.corrections or []),
                         {"field": field, "old": old, "new": new, "at": utcnow().isoformat()}]
    event.user_corrected = True


def _mirror_entry(db: Session, event: ApplicationCommunicationEvent) -> ApplicationEvent | None:
    return db.get(ApplicationEvent, event.application_event_id) if event.application_event_id else None


def review(db: Session, event: ApplicationCommunicationEvent, action: str) -> ApplicationCommunicationEvent:
    """accept_stage: apply the suggested stage (a person's decision); dismiss: hide it; mark_reviewed: done."""
    if action == "accept_stage":
        if event.stage_decision != "SUGGESTED" or not event.stage_to or not event.application_id:
            raise ValueError("There is no suggested stage change to accept")
        app = db.get(Application, event.application_id)
        ok, why = analysis.stage_policy.valid_transition(app.stage or "applied", event.stage_to)
        if not ok:
            raise ValueError(f"That stage change is no longer valid: {why}")
        tracker.enable_extra_stage(db, app, event.stage_to)
        tracker.set_stage(db, app, event.stage_to, meta={"source": "application_inbox_review",
                                                         "email_event_id": event.id})
        event.stage_decision, event.review_status = "APPLIED", "ACCEPTED"
        event.stage_reason = "accepted by you"
    elif action == "dismiss":
        event.review_status = "DISMISSED"
    elif action == "mark_reviewed":
        event.review_status, event.needs_review = "ACCEPTED", False
    else:
        raise ValueError("Unknown review action")
    db.commit()
    return event


def correct(db: Session, event: ApplicationCommunicationEvent, changes: dict[str, Any]) -> tuple[
        ApplicationCommunicationEvent, bool]:
    """Apply a person's corrections. Returns the event and whether it needs analysing again (a new application)."""
    reanalyse = False
    msg = db.get(ApplicationEmailMessage, event.message_id) if event.message_id else None
    if "application_id" in changes:
        new_id = changes["application_id"]
        if new_id is not None:
            target = db.get(Application, new_id)
            if target is None or target.applied_at is None:
                raise ValueError("Choose a submitted application")
        if new_id != event.application_id:
            _record(event, "application_id", event.application_id, new_id)
            old_entry = _mirror_entry(db, event)
            if old_entry is not None:
                db.delete(old_entry)
                event.application_event_id = None
            event.application_id = new_id
            if msg is not None:
                msg.application_id = new_id
                msg.match_method = "USER"
                msg.match_status = "MATCHED" if new_id else "IGNORED"
                msg.analysis_status = "PENDING" if new_id else "SKIPPED"
                msg.attempts, msg.next_attempt_at = 0, None
            if new_id is None:
                event.review_status = "DISMISSED"
                event.stage_decision, event.stage_to = "NONE", None
            else:
                event.needs_review = False
                reanalyse = True
    if "event_type" in changes and changes["event_type"] != event.event_type:
        if changes["event_type"] not in EMAIL_EVENT_TYPES:
            raise ValueError("Unknown event type")
        _record(event, "event_type", event.event_type, changes["event_type"])
        event.event_type, event.classification_method = changes["event_type"], "USER"
    if "deadline" in changes:
        new_deadline = changes["deadline"]
        if new_deadline != event.deadline:
            _record(event, "deadline", _iso(event.deadline), _iso(new_deadline))
            event.deadline = new_deadline
    if changes.get("stage"):
        stage = changes["stage"]
        if stage not in tracker.ALL_STAGE_IDS:
            raise ValueError("Unknown stage")
        if not event.application_id:
            raise ValueError("Link the email to an application before changing its stage")
        app = db.get(Application, event.application_id)
        _record(event, "stage", app.stage, stage)
        tracker.enable_extra_stage(db, app, stage)
        tracker.set_stage(db, app, stage, meta={"source": "application_inbox_correction", "email_event_id": event.id})
        event.stage_decision, event.stage_from, event.stage_to = "APPLIED", event.stage_from or app.stage, stage
        event.stage_reason = "set by you"
    if event.user_corrected and not reanalyse:
        event.needs_review = False
        if event.review_status == "OPEN":
            event.review_status = "ACCEPTED"
        entry = _mirror_entry(db, event)
        if entry is not None:
            entry.title = f"{classify.label(event.event_type)} · {entry.title.split(' · ', 1)[-1]}"[:300]
    db.commit()
    return event, reanalyse


def reanalyse_message(sessions: Any, user_id: int, message_id: int) -> None:
    """Background task after a person links an email to an application: analyse it now rather than next sync."""
    with sessions() as db:
        db.info["user_id"] = user_id
        msg = db.get(ApplicationEmailMessage, message_id)
        if msg is not None and msg.analysis_status == "PENDING":
            analysis.process_message(db, msg)


def event_types() -> list[dict[str, str]]:
    return [{"id": t, "label": classify.label(t)} for t in EMAIL_EVENT_TYPES]


# ───────────── monitoring ─────────────

def metrics(db: Session, user_id: int | None, start: datetime, end: datetime) -> dict[str, Any]:
    """Sync runs, matching, classification, stage changes and corrections for Monitoring (user-filtered)."""
    def owned(model: Any, query: Any) -> Any:
        return query.where(model.user_id == user_id) if user_id is not None else query

    runs = []
    for day_runs in db.scalars(owned(ApplicationInboxDigest, select(ApplicationInboxDigest.sync_runs).where(
            ApplicationInboxDigest.date >= start.date(), ApplicationInboxDigest.date <= end.date()))).all():
        runs += [r for r in day_runs or [] if start.isoformat() <= (r.get("at") or "") <= end.isoformat()]
    messages = db.execute(owned(ApplicationEmailMessage, select(
        ApplicationEmailMessage.match_status, ApplicationEmailMessage.match_method,
        ApplicationEmailMessage.analysis_status).where(ApplicationEmailMessage.created_at >= start,
                                                        ApplicationEmailMessage.created_at <= end))).all()
    events = db.scalars(owned(ApplicationCommunicationEvent, select(ApplicationCommunicationEvent).where(
        ApplicationCommunicationEvent.created_at >= start, ApplicationCommunicationEvent.created_at <= end))).all()
    confidences = [e.confidence for e in events if e.confidence is not None]
    total = lambda key: sum(int(r.get(key) or 0) for r in runs)
    return {
        "sync_runs": len(runs),
        "sync_failures": sum(1 for r in runs if r.get("status") not in ("succeeded", "partial")),
        "rate_limited_runs": sum(1 for r in runs if r.get("status") == "rate_limited"),
        "connection_required_runs": sum(1 for r in runs if r.get("status") == "connection_required"),
        "applications_checked": max((int(r.get("applications_checked") or 0) for r in runs), default=0),
        "messages_searched": total("messages_searched"), "messages_fetched": total("messages_fetched"),
        "messages_matched": sum(1 for m in messages if m.match_status == "MATCHED"),
        "messages_unmatched": sum(1 for m in messages if m.match_status in ("UNMATCHED", "IGNORED")),
        "messages_needs_review": sum(1 for m in messages if m.match_status == "NEEDS_REVIEW"),
        "deterministic_matches": sum(1 for m in messages if m.match_method == "DETERMINISTIC"),
        "llm_assisted_matches": sum(1 for m in messages if m.match_method == "LLM_ASSISTED"),
        "user_matches": sum(1 for m in messages if m.match_method == "USER"),
        "pending_analysis": sum(1 for m in messages if m.analysis_status == "PENDING"),
        "failed_analysis": sum(1 for m in messages if m.analysis_status == "FAILED"),
        "classifications": dict(sorted({t: sum(1 for e in events if e.event_type == t)
                                        for t in {e.event_type for e in events}}.items())),
        "classification_methods": {m: sum(1 for e in events if e.classification_method == m)
                                   for m in ("RULE", "LLM", "USER")},
        "stage_changes_applied": sum(1 for e in events if e.stage_decision == "APPLIED"),
        "stage_changes_suggested": sum(1 for e in events if e.stage_decision == "SUGGESTED"),
        "stage_changes_invalid": sum(1 for e in events if e.stage_decision == "INVALID"),
        "needs_review": sum(1 for e in events if e.needs_review and e.review_status == "OPEN"),
        "user_corrections": sum(len(e.corrections or []) for e in events),
        "mean_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "llm_failures": total("llm_failures"), "schema_failures": total("schema_failures"),
        "models": dict(Counter(e.model for e in events if e.model)),
        "prompt_versions": dict(Counter(e.prompt_version for e in events if e.prompt_version)),
        "followups_suppressed": total("followups_suppressed"),
        "gmail_requests": _gmail_requests(db, user_id, start, end),
    }


def _gmail_requests(db: Session, user_id: int | None, start: datetime, end: datetime) -> dict[str, int]:
    from app.models import ExternalAPIRequestLog

    query = (select(ExternalAPIRequestLog.status, ExternalAPIRequestLog.error_type, func.count())
             .where(ExternalAPIRequestLog.provider.in_(("GMAIL", "GOOGLE_OAUTH")),
                    ExternalAPIRequestLog.created_at >= start, ExternalAPIRequestLog.created_at <= end)
             .group_by(ExternalAPIRequestLog.status, ExternalAPIRequestLog.error_type))
    if user_id is not None:
        query = query.where(ExternalAPIRequestLog.user_id == user_id)
    out = {"total": 0, "failed": 0, "rate_limited": 0}
    for status_, error_type, count in db.execute(query).all():
        out["total"] += count
        out["failed"] += count if status_ == "FAILED" else 0
        out["rate_limited"] += count if error_type == "RATE_LIMIT" else 0
    return out

