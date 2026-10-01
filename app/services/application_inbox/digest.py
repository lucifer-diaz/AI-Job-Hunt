"""The daily Application Inbox digest and the Inbox Insights sections, built from the database — never by a model.

Wording stays neutral: an application with no reply is "waiting", described as "No new application-related email
detected since …". Silence is never reported as a rejection.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Application,
    ApplicationCommunicationEvent,
    ApplicationEmailMessage,
    ApplicationInboxDigest,
)
from app.services.application_inbox import classify
from app.services.application_inbox.analysis import interview_start
from app.services.tracker import INACTIVE_STAGES, stage_label

RECENT_DAYS = 30
UPCOMING_DAYS = 7
MAX_ITEMS = 50
MAX_RUNS = 12
INTERVIEW_TYPES = ("INTERVIEW_INVITATION", "INTERVIEW_RESCHEDULE")
ASSESSMENT_TYPES = ("ASSESSMENT_INVITATION", "ASSESSMENT_REMINDER")
OFFER_TYPES = ("OFFER", "OFFER_UPDATE")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def relative(moment: datetime, now: datetime) -> str:
    days = (moment.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 0:
        return f"{-days} day{'s' if days != -1 else ''} ago"
    return f"in {days} days"


def event_card(event: ApplicationCommunicationEvent, apps: dict[int, Application]) -> dict[str, Any]:
    app = apps.get(event.application_id) if event.application_id else None
    job = app.job if app else None
    return {
        "id": event.id, "application_id": event.application_id, "company": job.company if job else None,
        "role": job.title if job else None, "event_type": event.event_type, "label": classify.label(event.event_type),
        "sender": event.sender, "subject": event.subject,
        "received_at": event.received_at.isoformat() if event.received_at else None, "summary": event.summary,
        "requires_action": event.requires_action, "action_items": event.action_items or [],
        "deadline": event.deadline.isoformat() if event.deadline else None, "confidence": round(event.confidence, 2),
        "needs_review": event.needs_review, "review_status": event.review_status,
        "stage_decision": event.stage_decision, "stage_from": event.stage_from, "stage_to": event.stage_to,
        "stage_reason": event.stage_reason, "interview": event.interview, "assessment": event.assessment,
        "recruiter": event.recruiter, "classification_method": event.classification_method,
        "user_corrected": event.user_corrected, "gmail_thread_id": event.gmail_thread_id,
    }


def sections(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Inbox Insights for the session's account."""
    now = now or utcnow()
    since = now - timedelta(days=RECENT_DAYS)
    E = ApplicationCommunicationEvent
    # Recent email, plus anything older that is still waiting for the person.
    waiting_on_you = (E.review_status == "OPEN") & (E.needs_review | E.requires_action | E.application_id.is_(None)
                                                    | (E.stage_decision == "SUGGESTED"))
    events = db.scalars(select(E).where((E.received_at >= since) | E.received_at.is_(None) | waiting_on_you)
                        .order_by(E.received_at.desc())).all()
    apps = {a.id: a for a in db.scalars(select(Application).options(joinedload(Application.job))
                                        .where(Application.applied_at.is_not(None))).unique().all()}
    cards = [event_card(e, apps) for e in events if e.review_status != "DISMISSED"]
    by_id = {e.id: e for e in events}
    recent_cards = [c for c in cards if by_id[c["id"]].received_at is None or by_id[c["id"]].received_at >= since]

    def open_item(card: dict[str, Any]) -> bool:
        return card["review_status"] == "OPEN" and (
            card["needs_review"] or card["requires_action"] or card["stage_decision"] == "SUGGESTED"
            or card["application_id"] is None)

    day_ago = now - timedelta(days=1)
    recent = [c for c in cards if by_id[c["id"]].created_at >= day_ago]
    upcoming = []
    for card in cards:
        event = by_id[card["id"]]
        start = interview_start(event.interview) if event.event_type in INTERVIEW_TYPES else None
        if start and now <= start <= now + timedelta(days=UPCOMING_DAYS):
            upcoming.append({**card, "when": start.isoformat(), "what": f"{card['label']} {relative(start, now)}"})
        elif event.deadline and now - timedelta(days=1) <= event.deadline <= now + timedelta(days=UPCOMING_DAYS):
            upcoming.append({**card, "when": event.deadline.isoformat(),
                             "what": f"{card['label']} due {relative(event.deadline, now)}"})
    upcoming.sort(key=lambda c: c["when"])
    stage_changes = [c for c in cards if c["stage_decision"] == "APPLIED" and by_id[c["id"]].updated_at >= day_ago]
    return {
        "needs_attention": [c for c in cards if open_item(c)][:MAX_ITEMS],
        "upcoming": upcoming[:MAX_ITEMS],
        "new_updates": recent[:MAX_ITEMS],
        "stage_changes": [{**c, "change": f"{stage_label(c['stage_from'] or 'applied')} → {stage_label(c['stage_to'])}"}
                          for c in stage_changes][:MAX_ITEMS],
        "interviews": [c for c in recent_cards if c["event_type"] in INTERVIEW_TYPES][:MAX_ITEMS],
        "assessments": [c for c in recent_cards if c["event_type"] in ASSESSMENT_TYPES][:MAX_ITEMS],
        "offers": [c for c in recent_cards if c["event_type"] in OFFER_TYPES][:MAX_ITEMS],
        "rejections": [c for c in recent_cards if c["event_type"] == "APPLICATION_REJECTION"][:MAX_ITEMS],
        "waiting": waiting(db, list(apps.values())),
        "new_response_counts": dict(Counter(c["event_type"] for c in recent)),
    }


def waiting(db: Session, apps: list[Application]) -> list[dict[str, Any]]:
    """Live applications with no reply from the employer since they were submitted (or last followed up)."""
    replied = set(db.scalars(select(ApplicationCommunicationEvent.application_id).where(
        ApplicationCommunicationEvent.application_id.is_not(None),
        ApplicationCommunicationEvent.event_type.in_(classify.HUMAN_REPLY_TYPES),
        ApplicationCommunicationEvent.review_status != "DISMISSED")).all())
    out = []
    for app in sorted((a for a in apps if a.applied_at), key=lambda a: a.applied_at):
        if app.id in replied or (app.stage or "applied") in INACTIVE_STAGES or app.status != "applied":
            continue
        if (app.stage or "applied") not in ("applied", "acknowledged"):
            continue
        since = app.applied_at
        job = app.job
        out.append({"application_id": app.id, "company": job.company if job else "", "role": job.title if job else "",
                    "stage": app.stage or "applied", "since": since.isoformat() if since else None,
                    "text": "No new application-related email detected"
                            + (f" since {since.strftime('%-d %b')}." if since else ".")})
    return out[:MAX_ITEMS]


def summary_text(counts: Counter, needs_attention: int, waiting_count: int) -> str:
    total = sum(counts.values())
    if not total:
        text = "No new application-related email detected."
    else:
        parts = [f"{n} {classify.label(kind).lower()}" for kind, n in counts.most_common()]
        text = f"{total} new application update{'s' if total != 1 else ''}: {', '.join(parts)}."
    if needs_attention:
        text += f" {needs_attention} need{'s' if needs_attention == 1 else ''} your attention."
    if waiting_count:
        text += f" {waiting_count} application{'s' if waiting_count != 1 else ''} still waiting for a reply."
    return text


def _digest_row(db: Session, user_id: int, day: date) -> ApplicationInboxDigest:
    row = db.scalar(select(ApplicationInboxDigest).where(ApplicationInboxDigest.date == day))
    if row is None:
        row = ApplicationInboxDigest(user_id=user_id, date=day, summary="")
        db.add(row)
    return row


def record_run(db: Session, user_id: int, now: datetime, run: dict[str, Any]) -> ApplicationInboxDigest:
    row = _digest_row(db, user_id, now.date())
    row.sync_runs = [*(row.sync_runs or []), run][-MAX_RUNS:]
    db.flush()
    return row


def build(db: Session, user_id: int, now: datetime, run: dict[str, Any]) -> ApplicationInboxDigest:
    """Write today's digest after a successful sync (a later sync the same day rewrites it)."""
    data = sections(db, now)
    counts = Counter(data["new_response_counts"])
    row = record_run(db, user_id, now, run)
    runs = row.sync_runs or []
    row.applications_checked = run.get("applications_checked", 0)
    row.messages_scanned = sum(r.get("messages_new", 0) for r in runs)
    row.messages_matched = sum(r.get("messages_matched", 0) for r in runs)
    row.new_updates = sum(counts.values())
    row.needs_action_count = len(data["needs_attention"])
    row.interviews = sum(counts.get(t, 0) for t in INTERVIEW_TYPES)
    row.assessments = sum(counts.get(t, 0) for t in ASSESSMENT_TYPES)
    row.rejections = counts.get("APPLICATION_REJECTION", 0)
    row.offers = sum(counts.get(t, 0) for t in OFFER_TYPES)
    row.summary = summary_text(counts, row.needs_action_count, len(data["waiting"]))
    brief = ("id", "application_id", "company", "role", "label", "deadline", "summary")
    row.sections = {
        "needs_attention": [{k: c.get(k) for k in brief} for c in data["needs_attention"][:20]],
        "stage_changes": [{"application_id": c["application_id"], "company": c["company"], "role": c["role"],
                           "change": c["change"]} for c in data["stage_changes"][:20]],
        "new_responses": data["new_response_counts"],
        "upcoming": [{"application_id": c["application_id"], "company": c["company"], "what": c["what"],
                      "when": c["when"]} for c in data["upcoming"][:20]],
        "waiting": data["waiting"][:20],
    }
    row.updated_at = now
    db.flush()
    return row


def latest(db: Session) -> ApplicationInboxDigest | None:
    return db.scalar(select(ApplicationInboxDigest).order_by(ApplicationInboxDigest.date.desc()).limit(1))


def digest_dict(row: ApplicationInboxDigest | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row.id, "date": row.date.isoformat(), "applications_checked": row.applications_checked,
            "messages_scanned": row.messages_scanned, "messages_matched": row.messages_matched,
            "new_updates": row.new_updates, "needs_action_count": row.needs_action_count,
            "interviews": row.interviews, "assessments": row.assessments, "rejections": row.rejections,
            "offers": row.offers, "summary": row.summary, "sections": row.sections or {},
            "sync_runs": row.sync_runs or [], "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat()}


def pending_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(ApplicationEmailMessage.analysis_status, ApplicationEmailMessage.match_status)).all()
    return {"pending_analysis": sum(1 for a, _ in rows if a == "PENDING"),
            "needs_review_matches": sum(1 for _, m in rows if m == "NEEDS_REVIEW"),
            "messages_seen": len(rows)}
