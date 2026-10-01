"""From one stored, matched email to its place on the application's timeline.

    stored message (ApplicationEmailMessage, analysis PENDING)
      → phrase rules (classify) — settle receipts and explicit rejections without a model
      → otherwise the Application Inbox prompt through the LLM gateway (ApplicationEmailAnalysis, schema-validated)
      → verification against the email's own text (names, links, dates, time zones the email does not state are dropped)
      → ApplicationCommunicationEvent + a tracker timeline entry (+ an interview entry when fully stated)
      → stage_policy decides; only code changes the stage
      → a recruiter reply makes the pending follow-up unnecessary

A model failure never loses the email: the message stays PENDING and is retried; after the last attempt the rule's
reading (or UNKNOWN) is saved for a person to review. A person's corrections are never overwritten.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationCommunicationEvent,
    ApplicationEmailMessage,
    ApplicationEvent,
)
from app.prompts import build_application_email_match_prompt, build_application_inbox_prompt
from app.prompts.application_inbox import APPLICATION_INBOX_PROMPT_VERSION
from app.prompts.contracts import ApplicationEmailAnalysis
from app.services import tracker
from app.services.application_inbox import classify, stage_policy
from app.services.contacts import recruiters_of, set_recruiters
from app.services.llm import LLMCall, LLMClient, LLMError
from app.services.redaction import redact_text
from app.services.user_settings import ai_ready
from app.settings import get_settings

log = logging.getLogger("jobhunt.application_inbox")
SOURCE = "application_inbox"
# Abbreviations that name exactly one time zone family; anything else (IST, CST, BST) is ambiguous.
ZONE_WORDS = {
    "pst": ("America/Los_Angeles",), "pdt": ("America/Los_Angeles",), "pacific": ("America/Los_Angeles",),
    "est": ("America/New_York",), "edt": ("America/New_York",), "eastern": ("America/New_York",),
    "sgt": ("Asia/Singapore",), "jst": ("Asia/Tokyo",), "india": ("Asia/Kolkata", "Asia/Calcutta"),
    "kolkata": ("Asia/Kolkata",), "aest": ("Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane"),
    "gmt": ("UTC", "Etc/UTC", "Europe/London", "GMT"), "utc": ("UTC", "Etc/UTC"),
    "cet": ("Europe/",), "cest": ("Europe/",),
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def email_payload(msg: ApplicationEmailMessage) -> dict[str, Any]:
    return {"sender": msg.sender, "subject": msg.subject, "body": msg.relevant_text or msg.snippet,
            "received_at": msg.received_at.isoformat() if msg.received_at else None}


def application_facts(app: Application) -> dict[str, str]:
    job = app.job
    return {"company": job.company if job else "", "role": job.title if job else "",
            "current_stage": app.stage or "applied",
            "applied_at": app.applied_at.date().isoformat() if app.applied_at else ""}


# ───────────── model-assisted matching (review band only) ─────────────

def disambiguate(db: Session, msg: ApplicationEmailMessage, candidates: list[dict[str, Any]]) -> tuple[int | None, float]:
    """Ask the model which candidate the email is about. Only candidates deterministic matching found are shown, and
    only an id among them is accepted. Any failure means "no answer" (the person decides)."""
    if not candidates or not ai_ready(db, msg.user_id)[0]:
        return None, 0.0
    shown = [{"application_id": c["application_id"], "company": c.get("company"), "role": c.get("role")}
             for c in candidates]
    try:
        result = LLMClient.for_user(db).generate_prompt(
            build_application_email_match_prompt(shown, email_payload(msg)),
            LLMCall(workflow_type="application", entity_id=candidates[0]["application_id"]))
    except LLMError as exc:
        log.info("inbox match assistance unavailable: %s", type(exc).__name__)
        return None, 0.0
    answer = result.validated
    allowed = {c["application_id"] for c in candidates}
    if answer.matched_application_id not in allowed:
        return None, float(answer.confidence)
    return answer.matched_application_id, float(answer.confidence)


# ───────────── verification against the email itself ─────────────

def _contains(text: str, value: str | None) -> bool:
    return bool(value) and value.lower().strip() in text


def _zone_supported(zone: str, text: str) -> bool:
    if zone.upper().startswith(("UTC+", "UTC-", "+", "-")):
        return zone.upper().removeprefix("UTC") in text.upper().replace("GMT", "").replace("UTC", "")
    city = zone.rsplit("/", 1)[-1].replace("_", " ").lower()
    if city and city in text:
        return True
    for word, zones in ZONE_WORDS.items():
        if re.search(rf"(?<![a-z]){word}(?![a-z])", text) and any(zone == z or (z.endswith("/") and zone.startswith(z))
                                                                  for z in zones):
            return True
    return False


def _parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def verify(analysis: ApplicationEmailAnalysis, msg: ApplicationEmailMessage) -> tuple[ApplicationEmailAnalysis, list[str]]:
    """Keep only what the email itself supports. Returns the cleaned analysis and what was removed (→ review)."""
    text = f"{msg.sender}\n{msg.subject}\n{msg.relevant_text or msg.snippet}".lower()
    data = analysis.model_dump()
    dropped: list[str] = []
    floor = (msg.received_at or utcnow()) - timedelta(days=1)
    recruiter = data.get("recruiter")
    if recruiter:
        for key in ("name", "email"):
            if recruiter.get(key) and not _contains(text, recruiter[key]):
                recruiter[key] = None
                dropped.append(f"recruiter {key} not in the email")
        if not (recruiter.get("name") or recruiter.get("email")):
            data["recruiter"] = None
    interview = data.get("interview")
    if interview:
        if interview.get("meeting_link") and not _contains(text, interview["meeting_link"]):
            interview["meeting_link"] = None
            dropped.append("meeting link not in the email")
        if interview.get("interviewer_name") and not _contains(text, interview["interviewer_name"]):
            interview["interviewer_name"] = None
            dropped.append("interviewer not named in the email")
        if interview.get("timezone") and not _zone_supported(interview["timezone"], text):
            interview["timezone"] = None
            dropped.append("time zone not stated unambiguously")
        if interview.get("date") and datetime.fromisoformat(interview["date"]) < floor.replace(hour=0, minute=0):
            interview["date"] = interview["time"] = None
            dropped.append("interview date before the email")
    for holder in [*(data.get("action_items") or []), data.get("assessment") or {}]:
        deadline = _parse_deadline(holder.get("deadline"))
        if holder.get("deadline") and (deadline is None or deadline < floor.replace(hour=0, minute=0)):
            holder["deadline"] = None
            dropped.append("deadline before the email")
    if dropped:
        data["needs_review"] = True
    return ApplicationEmailAnalysis.model_validate(data), dropped


def earliest_deadline(analysis: ApplicationEmailAnalysis) -> datetime | None:
    values = [_parse_deadline(item.deadline) for item in analysis.action_items]
    if analysis.assessment:
        values.append(_parse_deadline(analysis.assessment.deadline))
    values = [v for v in values if v]
    return min(values) if values else None


def interview_start(interview: dict[str, Any] | None) -> datetime | None:
    """The interview's start in UTC, only when date, time and time zone are all stated."""
    if not interview or not (interview.get("date") and interview.get("time") and interview.get("timezone")):
        return None
    local = datetime.fromisoformat(f"{interview['date']}T{interview['time']}")
    zone_name = interview["timezone"]
    offset = re.fullmatch(r"(?:UTC)?([+-])(\d{2}):(\d{2})", zone_name.upper())
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        tz: Any = timezone(sign * timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3))))
    else:
        tz = ZoneInfo("UTC" if zone_name.upper() == "UTC" else zone_name)
    return local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


# ───────────── persistence ─────────────

def event_for(db: Session, gmail_message_id: str) -> ApplicationCommunicationEvent | None:
    return db.scalar(select(ApplicationCommunicationEvent)
                     .where(ApplicationCommunicationEvent.gmail_message_id == gmail_message_id))


def corrected_fields(event: ApplicationCommunicationEvent | None) -> set[str]:
    return {c.get("field") for c in (event.corrections or [])} if event else set()


def _mirror(db: Session, app: Application, event: ApplicationCommunicationEvent) -> None:
    """The tracker timeline entry for this email, created once and kept in step with the event."""
    who = re.sub(r"\s*<[^>]*>", "", event.sender or "").strip() or event.sender
    title = f"{classify.label(event.event_type)} · {who}"[:300]
    detail = (event.summary or "")[:4000]
    entry = db.get(ApplicationEvent, event.application_event_id) if event.application_event_id else None
    if entry is not None and entry.application_id == app.id:
        entry.title, entry.detail = title, detail
        return
    entry = ApplicationEvent(application=app, kind="email", title=title, detail=detail,
                             occurred_at=event.received_at or utcnow(),
                             meta={"source": SOURCE, "email_event_id": event.id, "gmail_thread_id": event.gmail_thread_id})
    db.add(entry)
    db.flush()
    event.application_event_id = entry.id


def _schedule_interview(db: Session, app: Application, event: ApplicationCommunicationEvent) -> None:
    """A tracker interview entry, only for a confident email that states the date, time and time zone."""
    start = interview_start(event.interview)
    if start is None or event.needs_review:
        return
    interview = event.interview or {}
    meta = {"source": SOURCE, "email_event_id": event.id, "timezone": interview["timezone"],
            "contact_name": interview.get("interviewer_name") or "", "gmail_thread_id": event.gmail_thread_id}
    existing = next((e for e in app.events if e.kind == "interview" and (e.meta or {}).get("source") == SOURCE
                     and (e.meta or {}).get("gmail_thread_id") == event.gmail_thread_id), None)
    title = (interview.get("round") or "Interview")[:300]
    if existing is not None:  # a reschedule in the same thread moves the interview, it does not add a second one
        existing.occurred_at, existing.title, existing.meta = start, title, {**(existing.meta or {}), **meta}
        return
    db.add(ApplicationEvent(application=app, kind="interview", title=title, occurred_at=start,
                            detail=" · ".join(filter(None, [interview.get("format"), interview.get("meeting_link")])),
                            meta={k: v for k, v in meta.items() if v}))


def _update_recruiter(app: Application, event: ApplicationCommunicationEvent, sender_email: str) -> None:
    """Add a recruiter the email explicitly names (name and address, from the sender's own domain)."""
    recruiter = event.recruiter or {}
    name, email = (recruiter.get("name") or "").strip(), (recruiter.get("email") or "").strip().lower()
    if not (name and email) or event.needs_review:
        return
    if email.split("@", 1)[-1] != (sender_email or "").split("@", 1)[-1]:
        return
    if re.match(r"^(no-?reply|do-?not-?reply|notifications?)", email):
        return
    current = recruiters_of(app)
    if any((r.get("email") or "").lower() == email for r in current):
        return
    set_recruiters(app, [*current, {"name": name[:200], "email": email[:300], "phone": ""}])


def _user_changed_stage_at(app: Application) -> datetime | None:
    moves = [e.created_at for e in app.events if e.kind == "stage" and (e.meta or {}).get("source") != SOURCE]
    return max(moves) if moves else None


def suppress_followup(db: Session, app: Application, event: ApplicationCommunicationEvent) -> bool:
    """A reply from the employer makes the pending follow-up unnecessary: the due date is cleared, with a note."""
    if event.event_type not in classify.HUMAN_REPLY_TYPES or app.followup_due_at is None:
        return False
    app.followup_due_at = None
    db.add(ApplicationEvent(application=app, kind="note", title="Follow-up not needed",
                            detail=f"{classify.label(event.event_type)} received"
                                   f"{' on ' + event.received_at.strftime('%-d %b') if event.received_at else ''}; "
                                   "the scheduled follow-up was cancelled.",
                            meta={"source": SOURCE, "email_event_id": event.id}))
    return True


def save_event(db: Session, msg: ApplicationEmailMessage, app: Application | None, analysis: ApplicationEmailAnalysis,
               *, method: str, model: str | None, prompt_version: str | None, explicit: bool,
               counters: dict[str, int] | None = None) -> ApplicationCommunicationEvent:
    """Create or refresh the event for this message, apply the stage policy and the timeline. Flushes only."""
    counters = counters if counters is not None else {}
    event = event_for(db, msg.gmail_message_id)
    locked = corrected_fields(event)
    if event is None:
        event = ApplicationCommunicationEvent(gmail_message_id=msg.gmail_message_id, user_id=msg.user_id)
        db.add(event)
    event.message_id, event.gmail_thread_id = msg.id, msg.gmail_thread_id
    if "application_id" not in locked:
        event.application_id = app.id if app else None
    event.sender, event.subject, event.received_at = msg.sender, msg.subject, msg.received_at
    values = {
        "event_type": analysis.event_type, "summary": redact_text(analysis.summary, 500) or "",
        "requires_action": analysis.requires_user_action,
        "action_items": [item.model_dump() for item in analysis.action_items] or None,
        "deadline": earliest_deadline(analysis),
        "interview": analysis.interview.model_dump() if analysis.interview else None,
        "assessment": analysis.assessment.model_dump() if analysis.assessment else None,
        "recruiter": analysis.recruiter.model_dump() if analysis.recruiter else None,
        "important_facts": analysis.important_facts or None, "confidence": float(analysis.confidence),
        "detected_stage": analysis.application_stage_detected,
        "recommended_tracker_action": analysis.recommended_tracker_action,
        "needs_review": analysis.needs_review or msg.match_status != "MATCHED",
    }
    for key, value in values.items():
        if key not in locked:
            setattr(event, key, value)
    event.classification_method = "USER" if "event_type" in locked else method
    event.model, event.prompt_version = model, prompt_version
    db.flush()
    if app is None:
        event.stage_decision, event.stage_reason = "NONE", "which application this email is about needs review"
        return event
    _mirror(db, app, event)
    if "stage" not in locked:
        decision = stage_policy.decide(
            analysis if "event_type" not in locked else analysis.model_copy(update={"event_type": event.event_type}),
            current=app.stage or "applied", match_status=msg.match_status, explicit=explicit,
            user_changed_after=_user_changed_stage_at(app), received_at=msg.received_at,
            threshold=get_settings().application_stage_auto_update_threshold)
        event.stage_decision, event.stage_from, event.stage_to = decision.decision, decision.stage_from, decision.stage_to
        event.stage_reason = decision.reason[:300]
        if decision.decision == stage_policy.APPLIED and decision.stage_to:
            apply_stage(db, app, event, decision.stage_to)
            counters["stage_applied"] = counters.get("stage_applied", 0) + 1
        elif decision.decision == stage_policy.SUGGESTED:
            counters["stage_suggested"] = counters.get("stage_suggested", 0) + 1
    _schedule_interview(db, app, event)
    _update_recruiter(app, event, msg.sender.split("<")[-1].rstrip(">").strip().lower() if msg.sender else "")
    if suppress_followup(db, app, event):
        counters["followups_suppressed"] = counters.get("followups_suppressed", 0) + 1
    return event


def apply_stage(db: Session, app: Application, event: ApplicationCommunicationEvent, stage: str) -> None:
    tracker.enable_extra_stage(db, app, stage)
    tracker.set_stage(db, app, stage, meta={"source": SOURCE, "email_event_id": event.id},
                      title=f"Moved to {tracker.stage_label(stage)} (from email)")
    event.stage_decision = stage_policy.APPLIED


def process_message(db: Session, msg: ApplicationEmailMessage, *, now: datetime | None = None,
                    counters: dict[str, int] | None = None) -> str:
    """Analyse one PENDING message. Returns completed | retrying | fallback | skipped. Commits."""
    now = now or utcnow()
    counters = counters if counters is not None else {}
    settings = get_settings()
    app = db.get(Application, msg.application_id) if msg.application_id else None
    if app is None or msg.match_status != "MATCHED":
        msg.analysis_status = "SKIPPED"
        db.commit()
        return "skipped"
    rule = classify.match_rule(msg.subject, msg.relevant_text or msg.snippet)
    method, model, version = "RULE", None, None
    outcome = "completed"
    if rule is not None and rule.settles:
        analysis = classify.rule_analysis(rule, needs_review=False)
        counters["rule_classifications"] = counters.get("rule_classifications", 0) + 1
    elif not ai_ready(db, msg.user_id)[0]:
        analysis = (classify.rule_analysis(rule, needs_review=True) if rule
                    else classify.unknown_analysis(msg.automated))
        outcome = "fallback"
    else:
        try:
            prompt = build_application_inbox_prompt(application_facts(app), email_payload(msg),
                                                    rule_hint=rule.event_type if rule else None)
            result = LLMClient.for_user(db).generate_prompt(prompt, LLMCall(workflow_type="application",
                                                                            entity_id=app.id, attempt=msg.attempts + 1))
        except LLMError as exc:
            msg.attempts += 1
            msg.last_error = redact_text(f"{type(exc).__name__}: {exc}", 1000)
            counters["llm_failures"] = counters.get("llm_failures", 0) + 1
            if getattr(exc, "reason", "") in ("WRONG_OUTPUT_SCHEMA", "POSSIBLE_CROSS_WORKER_OUTPUT", "INVALID_JSON"):
                counters["schema_failures"] = counters.get("schema_failures", 0) + 1
            if msg.attempts < max(1, settings.application_inbox_analysis_max_attempts):
                msg.analysis_status = "PENDING"
                msg.next_attempt_at = now + timedelta(seconds=settings.application_inbox_retry_seconds * msg.attempts)
                db.commit()
                return "retrying"
            analysis = (classify.rule_analysis(rule, needs_review=True) if rule
                        else classify.unknown_analysis(msg.automated))
            outcome = "fallback"
        else:
            analysis, method, model, version = result.validated, "LLM", result.model, prompt.version
            counters["llm_classifications"] = counters.get("llm_classifications", 0) + 1
    analysis, dropped = verify(analysis, msg)
    explicit = rule is not None and classify.family(rule.event_type) == classify.family(analysis.event_type)
    save_event(db, msg, app, analysis, method=method, model=model, prompt_version=version or (
        APPLICATION_INBOX_PROMPT_VERSION if method == "LLM" else None), explicit=explicit, counters=counters)
    msg.analysis_status = "COMPLETED" if outcome == "completed" else "FAILED" if msg.attempts else "COMPLETED"
    msg.next_attempt_at = None
    if dropped:
        msg.last_error = redact_text("Removed from the analysis: " + "; ".join(dropped), 1000)
    db.commit()
    counters["analyzed"] = counters.get("analyzed", 0) + 1
    return outcome
