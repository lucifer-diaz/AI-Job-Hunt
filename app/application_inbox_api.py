"""Application Inbox API: connect Gmail (read-only), sync in the background, read insights, review and correct.

No endpoint here reads Gmail or calls a model while the request waits: a sync is queued as a background task (the
account's sync lease keeps it from overlapping the scheduler), and so is re-analysis after a correction.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.models import Application, ApplicationCommunicationEvent
from app.services import gmail
from app.services.application_inbox import digest
from app.services.application_inbox import service as inbox
from app.services.application_inbox.sync import sync_account
from app.settings import get_settings

router = APIRouter()


def _sessions():
    from app.db import SessionLocal

    return SessionLocal


# ───────────── Gmail connection ─────────────

@router.get("/api/gmail/connect")
def gmail_connect(user: CurrentUser = Depends(current_user)):
    """Send the browser to Google's consent screen (scope gmail.readonly only)."""
    try:
        return RedirectResponse(gmail.authorization_url(user.id), status_code=303)
    except gmail.GmailNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/api/gmail/oauth/callback")
def gmail_callback(code: str = "", state: str = "", error: str = "",
                   user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    """Google returns here. The state must have been issued to the signed-in account; only the encrypted refresh
    token and the mailbox address are kept."""
    if error:
        return RedirectResponse("/#settings/gmail", status_code=303)
    try:
        if gmail.read_state(state) != user.id:
            raise ValueError("this Gmail authorization was started by another account")
        gmail.exchange_code(db, user.id, code)
    except (ValueError, gmail.GmailError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/#settings/gmail", status_code=303)


@router.delete("/api/gmail/connection")
def gmail_disconnect(db: Session = Depends(get_user_db)):
    conn = gmail.connection_for(db)
    if conn is not None:
        gmail.disconnect(db, conn)
    return inbox.status(db)


# ───────────── sync and insights ─────────────

@router.post("/api/application-inbox/sync", status_code=202)
def inbox_sync(background: BackgroundTasks, user: CurrentUser = Depends(current_user),
               db: Session = Depends(get_user_db)):
    """Queue a sync of this account's Gmail. Returns at once; poll GET /api/application-inbox/status."""
    if not get_settings().gmail_oauth_configured:
        raise HTTPException(503, "Gmail is not set up on this server")
    conn = gmail.connection_for(db)
    if conn is None or conn.status != gmail.CONNECTED:
        raise HTTPException(409, "Connect Gmail in Settings first")
    background.add_task(sync_account, _sessions(), user.id, trigger="manual")
    return {"queued": True, **inbox.status(db)}


@router.get("/api/application-inbox/status")
def inbox_status(db: Session = Depends(get_user_db)):
    return inbox.status(db)


@router.get("/api/application-inbox/digests/latest")
def inbox_latest_digest(db: Session = Depends(get_user_db)):
    return {"digest": digest.digest_dict(digest.latest(db))}


@router.get("/api/application-inbox/insights")
def inbox_insights(db: Session = Depends(get_user_db)):
    return {"sections": digest.sections(db), "digest": digest.digest_dict(digest.latest(db)),
            "status": inbox.status(db), "event_types": inbox.event_types()}


@router.get("/api/applications/{application_id}/email-events")
def application_email_events(application_id: int, db: Session = Depends(get_user_db)):
    if db.get(Application, application_id) is None:
        raise HTTPException(404, "Application not found")
    return {"events": inbox.events_for_application(db, application_id)}


# ───────────── review and correction ─────────────

def _event(db: Session, event_id: int) -> ApplicationCommunicationEvent:
    event = db.get(ApplicationCommunicationEvent, event_id)
    if event is None:
        raise HTTPException(404, "Email event not found")
    return event


class ReviewIn(BaseModel):
    action: Literal["accept_stage", "dismiss", "mark_reviewed"]


@router.post("/api/application-email-events/{event_id}/review")
def review_email_event(event_id: int, payload: ReviewIn, db: Session = Depends(get_user_db)):
    try:
        event = inbox.review(db, _event(db, event_id), payload.action)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"event": digest.event_card(event, _apps(db, event))}


class CorrectionIn(BaseModel):
    """Only the fields sent are changed. application_id null means "not about any of my applications"."""

    application_id: int | None = None
    event_type: str | None = Field(default=None, max_length=40)
    stage: str | None = Field(default=None, max_length=50)
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def _naive_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC).replace(tzinfo=None) if value and value.tzinfo else value


@router.post("/api/application-email-events/{event_id}/correct")
def correct_email_event(event_id: int, payload: CorrectionIn, background: BackgroundTasks,
                        user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)):
    changes = {name: getattr(payload, name) for name in payload.model_fields_set}
    if not changes:
        raise HTTPException(422, "Nothing to correct")
    try:
        event, reanalyse = inbox.correct(db, _event(db, event_id), changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if reanalyse and event.message_id:
        background.add_task(inbox.reanalyse_message, _sessions(), user.id, event.message_id)
    return {"event": digest.event_card(event, _apps(db, event)), "reanalysing": reanalyse}


def _apps(db: Session, event: ApplicationCommunicationEvent) -> dict[int, Application]:
    app = db.get(Application, event.application_id) if event.application_id else None
    return {app.id: app} if app else {}
