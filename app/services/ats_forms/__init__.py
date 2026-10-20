"""Application forms as Job Hunt OS reads them (Phase 5): a typed structure, its fingerprint, and the adapter for
the site that serves it.

    analyse(url, html)  → the adapter whose `detect` matches, its `ApplicationForm` (fields, required, options, where
                          each field is on the page) — or why the page cannot be used (a login wall, an OTP, a
                          challenge, a closed posting, no known form)

Adapters are deterministic HTML readers. They never submit, and they never decide an answer: the AnswerResolver
(app.services.answer_resolver) does, from verified sources only. Page text is untrusted data; nothing in it is
followed. The form's submit control is recorded as present/absent and its label only, so no code can act on it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from app.services import jd_quality

FIELD_KINDS = ("text", "email", "tel", "url", "number", "textarea", "select", "radio", "checkbox", "file")
# What a field asks for, when the adapter can tell from the form's own markup; anything else is a "question".
ROLES = ("first_name", "last_name", "full_name", "email", "phone", "resume", "cover_letter", "linkedin", "github",
         "website", "location", "question")
# Why a page is not a usable form (ApplicationExecutionTask.interrupt.reason / result_code).
LOGIN_REQUIRED, OTP_REQUIRED, CAPTCHA = "LOGIN_REQUIRED", "OTP_REQUIRED", "CAPTCHA_OR_ANTI_BOT"
POSTING_CLOSED, NO_FORM, UNSUPPORTED = "POSTING_CLOSED", "NO_FORM", "UNSUPPORTED_SITE"
HUMAN_BLOCKERS = (LOGIN_REQUIRED, OTP_REQUIRED, CAPTCHA)
OTP_MARKERS = ("one-time code", "one time code", "verification code", "enter the code", "6-digit code",
               "security code", "two-factor", "2fa", "one-time passcode", "authenticator app")


class FormOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=300)
    value: str = Field(max_length=300)


class FormField(BaseModel):
    """One question of the form. `locator` is a CSS selector the adapter derived from the field's own id or name."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=200)
    label: str = Field(max_length=500)
    kind: Literal["text", "email", "tel", "url", "number", "textarea", "select", "radio", "checkbox", "file"]
    role: Literal["first_name", "last_name", "full_name", "email", "phone", "resume", "cover_letter", "linkedin",
                  "github", "website", "location", "question"] = "question"
    required: bool = False
    options: list[FormOption] = Field(default_factory=list, max_length=200)
    locator: str = Field(max_length=400)
    eeo: bool = False  # voluntary self-identification: never answered automatically


class SubmitControl(BaseModel):
    """The form's final submit control: whether it exists and its label. Deliberately no locator."""

    model_config = ConfigDict(extra="forbid")
    present: bool
    label: str = Field(default="", max_length=120)


class ApplicationForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: str = Field(max_length=40)
    host: str = Field(max_length=200)
    fields: list[FormField] = Field(max_length=300)
    submit: SubmitControl
    fingerprint: str = Field(default="", max_length=64)

    def required_fields(self) -> list[FormField]:
        return [f for f in self.fields if f.required]


def fingerprint(fields: list[FormField]) -> str:
    """The form's shape: field ids, kinds, roles, required flags, labels and option values. A form whose shape
    changed between reading and filling is not the form that was planned for."""
    shape = [[f.id, f.kind, f.role, f.required, f.label, [o.value for o in f.options]] for f in fields]
    return hashlib.sha256(json.dumps(sorted(shape), ensure_ascii=False).encode("utf-8")).hexdigest()


def with_fingerprint(form: ApplicationForm) -> ApplicationForm:
    return form.model_copy(update={"fingerprint": fingerprint(form.fields)})


def page_problem(html: str, *, status: int | None = None, title: str | None = None) -> str | None:
    """Why this page cannot be filled: a login wall, an OTP prompt, an anti-bot challenge or a closed posting.
    Uses the same markers JD recovery uses (jd_quality); never retried, never worked around."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    has_password = soup.find("input", attrs={"type": "password"}) is not None
    lowered = text.lower()
    kind = jd_quality.classify_page(status=status, text=text, title=title, has_password_field=has_password)
    if kind == jd_quality.CAPTCHA_OR_ANTI_BOT:
        return CAPTCHA
    if soup.find("input", attrs={"autocomplete": "one-time-code"}) is not None or (
            len(lowered) < 4000 and any(marker in lowered for marker in OTP_MARKERS)):
        return OTP_REQUIRED
    if kind == jd_quality.LOGIN_REQUIRED or (has_password and len(lowered) < 4000):
        return LOGIN_REQUIRED
    if kind == jd_quality.JOB_REMOVED or jd_quality.closed_or_error(text, title):
        return POSTING_CLOSED
    return None


def adapters() -> list[Any]:
    from app.services.ats_forms import greenhouse

    return [greenhouse]


def adapter_for(url: str, html: str) -> Any | None:
    return next((a for a in adapters() if a.detect(url, html)), None)


def adapter_named(name: str) -> Any | None:
    return next((a for a in adapters() if a.NAME == name), None)
