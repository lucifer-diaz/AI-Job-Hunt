"""The verification code an applicant tracking system emailed after the final Submit, for the person to paste.

    find_code(db, task) → {"status": "FOUND", "code": ...} | {"status": <why not>, "message": ...}

Asked for only by the person (Apply kit → "Get code from Gmail"), for a guarded run whose click ended on a page that
asks for an emailed code (`submission.uncertain` with `email_code`). One targeted, read-only Gmail search: the ATS's
own sender word and mail sent after the click, newest first, a few messages. The code is taken from the message text
by a deterministic pattern next to "code" wording; no model sees the mail. It is returned to that one request and
never stored, logged, captured (`NO_CAPTURE`) or put in graph state. The run never enters it: the person does.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import ApplicationExecutionTask, Job
from app.services import gmail

FOUND, NOT_CONNECTED, NOT_WAITING, NOT_FOUND, UNAVAILABLE = (
    "FOUND", "NOT_CONNECTED", "NOT_WAITING", "NOT_FOUND", "UNAVAILABLE")
MAX_MESSAGES = 3
BODY_LIMIT = 4000
# Mail sent this long before the recorded click still counts: the click time is stamped by the worker, the mail by
# the provider, and their clocks differ.
CLOCK_SLACK = timedelta(minutes=2)
# Where a code is announced ("security code", "verification code", "your code", "passcode"), and the token that is the
# code: letters and digits (a hyphen between groups allowed), with at least one digit, so words never pass.
CODE_WORD = re.compile(r"\b(?:pass)?code\b|\bpin\b|\bOTP\b", re.IGNORECASE)
TOKEN = re.compile(r"(?<![A-Za-z0-9@./-])([A-Za-z0-9]{2,8}(?:-[A-Za-z0-9]{2,8}){0,3})(?![A-Za-z0-9@/-])")
WINDOW = 200  # characters after the wording the code may sit in ("…into the security code field on …: Wz3K9pQa")
SEARCH_WORDS = '("security code" OR "verification code" OR "confirmation code" OR "your code")'


def extract(text: str) -> str | None:
    """The code in an email's text, or None: the first token after code wording that has a digit and 4 to 16
    characters once hyphens are dropped."""
    text = text or ""
    for word in CODE_WORD.finditer(text):
        for token in TOKEN.finditer(text, word.end(), word.end() + WINDOW):
            candidate = token.group(1).replace("-", "")
            if 4 <= len(candidate) <= 16 and any(c.isdigit() for c in candidate):
                return candidate
    return None


def _clicked_at(db: Session, task: ApplicationExecutionTask) -> datetime | None:
    from app.services import submission

    intents = [i for i in submission.intents(db, task.id) if i.clicking_at is not None]
    return intents[-1].clicking_at if intents else None


def waiting(db: Session, task: ApplicationExecutionTask) -> bool:
    """The run's last click ended on a page that asks for an emailed code, and the outcome is still open."""
    from app.services import submission

    intents = submission.intents(db, task.id)
    last = intents[-1] if intents else None
    return last is not None and last.status == submission.UNCERTAIN and bool((last.evidence or {}).get("email_code"))


def query(job: Job, after: datetime) -> str:
    """The Gmail search: the ATS's sender word (greenhouse, lever, …) when the route names one, code wording, and only
    mail received after `after`. Never the whole mailbox."""
    from app.services import ats_map

    match = ats_map.for_job(job)
    sender = re.sub(r"[^a-z0-9]", "", (match.ats if match else "") or "")
    epoch = int(after.replace(tzinfo=UTC).timestamp())
    parts = [f"from:{sender}" if sender and sender != "generic" else "", SEARCH_WORDS, f"after:{epoch}"]
    return " ".join(p for p in parts if p)


def find_code(db: Session, task: ApplicationExecutionTask) -> dict[str, Any]:
    if not waiting(db, task):
        return {"status": NOT_WAITING, "message": "This run is not waiting for an emailed code"}
    job = db.get(Job, task.job_id)
    clicked = _clicked_at(db, task)
    conn = gmail.connection_for(db, task.user_id)
    if job is None or clicked is None:
        return {"status": NOT_WAITING, "message": "This run is not waiting for an emailed code"}
    if conn is None or conn.status != gmail.CONNECTED:
        return {"status": NOT_CONNECTED, "message": "Connect Gmail in Settings to look the code up here"}
    try:
        token = gmail.access_token(db, conn)
        with gmail.GmailClient(token, conn.user_id) as client:
            for message_id, _thread in client.list_message_ids(query(job, clicked - CLOCK_SLACK), MAX_MESSAGES):
                code = extract(gmail.body_text(client.get_full(message_id, no_capture=True), BODY_LIMIT))
                if code:
                    return {"status": FOUND, "code": code}
    except gmail.GmailConnectionRequired:
        return {"status": NOT_CONNECTED, "message": "Gmail needs to be connected again (Settings)"}
    except gmail.GmailError:
        return {"status": UNAVAILABLE, "message": "Gmail could not be read just now; try again in a moment"}
    return {"status": NOT_FOUND, "message": "No code email yet; try again in a moment"}
