"""Follow-ups to running applications in a browser window:

  * the code a site emails after Submit is found in Gmail (read-only, targeted, never kept) for the person to paste;
  * the new Greenhouse board's resume input ("Attach" in a "Resume/CV *" group) is the required resume;
  * Running workers' history reads every recorded step key (a takeover's included) instead of failing;
  * a window waiting for pointed-out fields continues only once they are all filled and stopped changing.
"""
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ExternalAPIRequestLog, GmailConnection, Job, SystemLogEntry
from app.security import encrypt_secret
from app.services import application_execution, ats_forms, gmail, workers
from app.services.application_inbox import codes
from app.services.ats_forms import generic
from app.settings import get_settings
from tests.conftest import make_user
from tests.gmail_fake import CLIENT_SECRET, REFRESH_TOKEN, FakeGoogle

GREENHOUSE_PAGE = ("A verification code was sent to asha@example.test. To submit your application, enter the "
                   "8-character code to confirm you're a human. Security code")
GREENHOUSE_MAIL = ("Copy and paste this code into the security code field on your application:\n\n Wz3K9pQa\n\n"
                   "This code expires in 10 minutes.")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ───────────── the emailed code ─────────────

@pytest.mark.parametrize("text, code", [
    ("Your security code is: 88661052. Enter it to submit.", "88661052"),
    (GREENHOUSE_MAIL, "Wz3K9pQa"),
    ("Your verification code\n\n 4821-77", "482177"),
    ("Your code expires in 10 minutes: 77AB91CD", "77AB91CD"),
    ("Thanks for applying, the code of conduct is below", None),
    ("Thanks for applying to Acme!", None),
])
def test_a_code_is_taken_only_next_to_code_wording_and_with_a_digit(text, code):
    assert codes.extract(text) == code


def test_the_greenhouse_page_after_submit_asks_for_an_emailed_code():
    assert ats_forms.asks_email_code(GREENHOUSE_PAGE)
    assert not ats_forms.asks_email_code("Thank you for applying. We received your application.")
    assert ats_forms.page_problem(f"<html><body><p>{GREENHOUSE_PAGE}</p></body></html>") == ats_forms.OTP_REQUIRED


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    gmail.use_transport(fake.transport())
    monkeypatch.setattr(gmail, "RETRY_SLEEP_SECONDS", 0)
    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", CLIENT_SECRET)
    yield fake
    gmail.use_transport(None)


def _waiting_run(monkeypatch, user_id: int, clicked: datetime) -> SimpleNamespace:
    with SessionLocal() as db:
        db.info["user_id"] = user_id
        job = Job(user_id=user_id, source="manual", source_ref=uuid.uuid4().hex, title="Engineer", company="Acme",
                  url="https://job-boards.greenhouse.io/acme/jobs/1", description="", status="approved")
        db.add(job)
        db.commit()
        job_id = job.id
    # The run's last click ended on the code page (an UNCERTAIN intent with `email_code`), at `clicked`.
    monkeypatch.setattr(codes, "waiting", lambda db, task: True)
    monkeypatch.setattr(codes, "_clicked_at", lambda db, task: clicked)
    return SimpleNamespace(id=1, user_id=user_id, job_id=job_id)


def _connect(user_id: int) -> None:
    with SessionLocal() as db:
        db.add(GmailConnection(user_id=user_id, email_address="me@example.com",
                               refresh_token_enc=encrypt_secret(REFRESH_TOKEN), scopes=gmail.GMAIL_READONLY_SCOPE,
                               status="CONNECTED", connected_at=utcnow()))
        db.commit()


def test_the_code_is_read_from_the_ats_mail_sent_after_the_click_and_never_kept(google, monkeypatch):
    user = make_user(SessionLocal)
    _connect(user)
    clicked = utcnow() - timedelta(minutes=1)
    task = _waiting_run(monkeypatch, user, clicked)
    google.add("old", sender="Greenhouse <no-reply@us.greenhouse-mail.io>", subject="Security code",
               body="Your security code is 11112222", received_at=clicked - timedelta(hours=3))
    google.add("new", sender="Greenhouse <no-reply@us.greenhouse-mail.io>", subject="Security code",
               body=GREENHOUSE_MAIL, received_at=clicked + timedelta(seconds=20))
    google.add("spoof", sender="Someone <x@example.com>", subject="code", body="Your code is 99998888",
               received_at=clicked + timedelta(seconds=30))
    with SessionLocal() as db:
        db.info["user_id"] = user
        found = codes.find_code(db, task)
    assert found == {"status": codes.FOUND, "code": "Wz3K9pQa"}
    query = google.queries[-1]
    assert "from:greenhouse" in query and f"after:{int((clicked - codes.CLOCK_SLACK).replace(tzinfo=UTC).timestamp())}" \
        in query and '"security code"' in query
    assert google.fetched("old") == 0 and google.fetched("spoof") == 0
    with SessionLocal() as db:
        rows = db.scalars(select(ExternalAPIRequestLog).where(ExternalAPIRequestLog.user_id == user)).all()
        assert rows and all(r.payload is None for r in rows)  # no body kept, whatever capture says
        logs = db.scalars(select(SystemLogEntry)).all()
        assert all("Wz3K9pQa" not in (str(r.message) + str(r.details)) for r in logs)


def test_no_gmail_or_no_mail_yet_says_so(google, monkeypatch):
    user = make_user(SessionLocal)
    task = _waiting_run(monkeypatch, user, utcnow())
    with SessionLocal() as db:
        db.info["user_id"] = user
        assert codes.find_code(db, task)["status"] == codes.NOT_CONNECTED
    _connect(user)
    with SessionLocal() as db:
        db.info["user_id"] = user
        assert codes.find_code(db, task)["status"] == codes.NOT_FOUND


def test_only_a_run_waiting_for_an_emailed_code_looks():
    with SessionLocal() as db:
        found = codes.find_code(db, SimpleNamespace(id=-1, user_id=1, job_id=-1))
    assert found["status"] == codes.NOT_WAITING and "code" not in found


# ───────────── the resume input on the new Greenhouse board ─────────────

def test_an_attach_button_in_a_resume_group_is_the_required_resume():
    elements = [
        {"tag": "input", "type": "file", "key": "r", "label": "Attach", "group_label": "Resume/CV*", "id": "resume"},
        {"tag": "input", "type": "file", "key": "c", "label": "Attach", "group_label": "Cover Letter",
         "id": "cover_letter"},
        {"tag": "input", "type": "file", "key": "o", "label": "Upload", "group_label": "Writing sample"},
        {"tag": "input", "type": "file", "key": "n", "label": "Attach", "name": "resume"},
    ]
    fields = {f.locator: f for f in generic.analyze("https://job-boards.greenhouse.io/x", elements).fields}
    assert (fields["key:r"].role, fields["key:r"].label, fields["key:r"].required) == ("resume", "Resume/CV", True)
    assert (fields["key:c"].role, fields["key:c"].required) == ("cover_letter", False)
    assert (fields["key:o"].role, fields["key:o"].label) == ("question", "Writing sample")
    assert fields["key:n"].role == "resume"  # by its own name, as the Greenhouse adapter reads it


# ───────────── Running workers history ─────────────

@pytest.mark.parametrize("key, words", [
    ("resolve_answers:14:takeover:a1:FIELDS_NEED_YOU:10", "Waited for you in the browser window (fields need you)"),
    ("resolve_answers:14:a1:2:1", "Saved 2 questions for you to answer and 1 to confirm"),
    ("resolve_answers:14:a1:0:0", "Found a verified source for every answer"),
    ("fill:3:takeover:a2:CAPTCHA_OR_ANTI_BOT:0", "Waited for you in the browser window (captcha or anti bot)"),
    ("resolve_answers:14:something:else:x", "Resolve answers"),
])
def test_every_recorded_step_key_reads_as_words(key, words):
    assert workers._step(key)[0] == words


# ───────────── a window whose fields the person filled ─────────────

def test_a_filled_window_counts_only_once_its_fields_stopped_changing(monkeypatch):
    monkeypatch.setattr(get_settings(), "application_fields_settle_seconds", 20)
    key = (1, 99)
    application_execution._fields_seen.pop(key, None)
    assert not application_execution.settled(key, None, now=0)  # a field is still empty
    assert not application_execution.settled(key, ("a", "b"), now=1)  # filled: the quiet period starts
    assert not application_execution.settled(key, ("a", "bc"), now=15)  # still typing: it starts again
    assert not application_execution.settled(key, ("a", "bc"), now=30)
    assert application_execution.settled(key, ("a", "bc"), now=36)
    assert not application_execution.settled(key, None, now=40)  # emptied again: forgotten
    application_execution._fields_seen.pop(key, None)


# ───────────── review follow-ups ─────────────

def test_the_code_search_is_never_widened_beyond_the_applications_own_sender(google, monkeypatch):
    """An employer's own site (generic route) is searched by its domain; with no domain to name, nothing is read."""
    user = make_user(SessionLocal)
    _connect(user)
    clicked = utcnow() - timedelta(minutes=1)
    task = _waiting_run(monkeypatch, user, clicked)
    with SessionLocal() as db:
        db.info["user_id"] = user
        job = db.get(Job, task.job_id)
        job.url = "https://careers.acme.co.uk/jobs/7/apply"
        db.commit()
    google.add("bank", sender="Bank <alerts@bank.example>", subject="Your security code",
               body="Your security code is 55554444", received_at=clicked + timedelta(seconds=10))
    google.add("acme", sender="Acme Careers <no-reply@acme.co.uk>", subject="Verify",
               body="Your verification code is 12345678", received_at=clicked + timedelta(seconds=20))
    with SessionLocal() as db:
        db.info["user_id"] = user
        assert codes.sender(db.get(Job, task.job_id)) == "acme.co.uk"
        assert codes.find_code(db, task) == {"status": codes.FOUND, "code": "12345678"}
        assert google.fetched("bank") == 0
        job = db.get(Job, task.job_id)
        job.url = "http://10.0.0.7/apply"
        db.commit()
        before = len(google.gmail_requests())
        assert codes.find_code(db, task)["status"] == codes.NO_SENDER
        assert len(google.gmail_requests()) == before  # declined: Gmail is not read at all


def test_a_grown_file_input_the_board_replaced_is_still_read_as_uploaded():
    """A required file input that appeared after the form was read, and that the board replaced with the uploaded
    file's name, is in neither form as read: its stored entry still reports the upload to the pre-submit checks."""
    import asyncio

    from app.agents import application
    from app.services.ats_forms import ApplicationForm, FormField, SubmitControl

    name = FormField(id="bu_n", label="Name", kind="text", role="full_name", required=True, locator="key:n")
    grown = FormField(id="bu_f", label="Portfolio", kind="file", role="resume", required=True, locator="key:f")
    planned = ApplicationForm(adapter="browser_use", host="x", fields=[name], submit=SubmitControl(present=True))

    class Session:
        async def call(self, cmd, **args):
            if cmd == "observe":
                return {"url": "https://x.example/apply", "html": "<form></form>", "title": "Apply",
                        "elements": [{"tag": "input", "type": "text", "key": "n", "label": "Name", "required": True}],
                        "attached": {"f": "resume.pdf"}}
            if cmd == "read":
                return {"files": ["resume.pdf"], "filled": True} if args["key"] == "f" else {"value": "Asha"}
            return {}

    ctx = SimpleNamespace(run_async=asyncio.run)
    facts = application._bu_facts(ctx, Session(), planned, {"bu_n": "Asha"}, [grown])
    assert facts["uploaded"] == {"bu_f": "resume.pdf"}
    assert application._bu_facts(ctx, Session(), planned, {"bu_n": "Asha"})["uploaded"] == {}
