"""Recruiter contacts on an application, and time zones for scheduled interviews.

An application keeps a list of recruiters (`Application.contacts`: name, email, phone). The first one is
mirrored into the older `recruiter_name` / `recruiter_email` columns, which follow-up drafts, the apply kit
and the agent tools still read.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import Application

MAX_RECRUITERS = 20
PHONE = re.compile(r"^\+?[0-9][0-9 ().\-]{3,30}$")


def clean_email(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise ValueError(f"{value} is not a valid email address") from exc


def clean_phone(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if value and not PHONE.match(value):
        raise ValueError(f"{value} is not a valid phone number: use digits, spaces, dashes and an optional leading +")
    return value


class ContactIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=300)
    phone: str = Field(default="", max_length=40)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return value.strip()

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return clean_email(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return clean_phone(value)

    @model_validator(mode="after")
    def _not_empty(self) -> ContactIn:
        if not (self.name or self.email or self.phone):
            raise ValueError("Each recruiter needs a name, email or phone number")
        return self


def recruiters_of(app: Application) -> list[dict[str, str]]:
    if isinstance(app.contacts, list):
        return [{"name": c.get("name", ""), "email": c.get("email", ""), "phone": c.get("phone", "")}
                for c in app.contacts if isinstance(c, dict)]
    if app.recruiter_name or app.recruiter_email:  # saved before multiple recruiters existed
        return [{"name": app.recruiter_name or "", "email": app.recruiter_email or "", "phone": ""}]
    return []


def set_recruiters(app: Application, recruiters: list[dict[str, str]]) -> None:
    app.contacts = [{"name": r.get("name", ""), "email": r.get("email", ""), "phone": r.get("phone", "")}
                    for r in recruiters][:MAX_RECRUITERS]
    first = app.contacts[0] if app.contacts else {}
    app.recruiter_name, app.recruiter_email = first.get("name", ""), first.get("email", "")


def set_primary_recruiter(app: Application, name: str, email: str) -> None:
    """Replace the first recruiter's name and email (the older single-recruiter API), keeping the others."""
    current = recruiters_of(app)
    name, email = (name or "").strip(), (email or "").strip()
    if not (name or email):
        set_recruiters(app, current[1:])
        return
    first = {**(current[0] if current else {"phone": ""}), "name": name, "email": email}
    set_recruiters(app, [first, *current[1:]])


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown time zone: {name}") from exc


def to_utc(local: datetime, tz_name: str) -> datetime:
    """A wall-clock time in a named time zone, as naive UTC (how event times are stored)."""
    aware = local.replace(tzinfo=zone(tz_name)) if local.tzinfo is None else local
    return aware.astimezone(UTC).replace(tzinfo=None)


def format_in_zone(utc_naive: datetime, tz_name: str | None, fmt: str = "%-d %b %H:%M") -> str:
    """Format a stored (naive UTC) time in the event's own time zone, with its abbreviation."""
    if not tz_name:
        return utc_naive.strftime(fmt)
    try:
        local = utc_naive.replace(tzinfo=UTC).astimezone(zone(tz_name))
    except ValueError:
        return utc_naive.strftime(fmt)
    return f"{local.strftime(fmt)} {local.strftime('%Z')}"


def event_meta(meta: dict[str, Any] | None) -> dict[str, str]:
    meta = meta or {}
    return {key: str(meta.get(key) or "") for key in ("timezone", "contact_name", "contact_email", "contact_phone")}
