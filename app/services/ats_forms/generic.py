"""Forms no deterministic adapter knows, read from the browser-use backend's observation of the page (Phase 6).

    analyze(url, elements) → ApplicationForm (adapter "browser_use") from the sidecar's indexed elements

The sidecar (sidecars/browser_use_runner) reports each interactive element's markup facts — tag, type, name, id,
its label text, required, options — never what it holds. This module turns them into the same typed form the
Greenhouse adapter produces, so the AnswerResolver decides every answer exactly as it does there (verified sources
only, no model). Each field's `locator` is `key:<element key>`: a stable hash of the element's own markup that the
sidecar finds again on a later observation, so a value can only ever go into the field it was resolved for.
Labels are page text: untrusted, used to classify and match, never followed.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.services.ats_forms import (
    ApplicationForm,
    FormField,
    FormOption,
    SubmitControl,
    is_legal,
    with_fingerprint,
)

NAME = "browser_use"
KEY_PREFIX = "key:"
TEXT_KINDS = ("text", "email", "tel", "url", "number")
EEO_MARKERS = ("gender", "race", "ethnicity", "veteran", "disability", "hispanic", "latino", "sexual orientation",
               "pronoun", "demographic", "self-identif", "eeo")
LABEL_ROLES = (
    ("first_name", re.compile(r"\b(first|given)\s*name\b")),
    ("last_name", re.compile(r"\b(last|family)\s*name\b|\bsurname\b")),
    ("full_name", re.compile(r"^(full\s*)?name\b|\byour name\b|\blegal name\b")),
    ("email", re.compile(r"\be-?mail\b")),
    ("phone", re.compile(r"\b(phone|mobile|telephone)\b")),
    ("linkedin", re.compile(r"linkedin")),
    ("github", re.compile(r"github")),
    ("website", re.compile(r"\b(website|portfolio|personal site)\b")),
    ("location", re.compile(r"\b(location|city|where are you based)\b")),
)


def locator(key: str) -> str:
    return f"{KEY_PREFIX}{key}"


def key_of(field_locator: str) -> str | None:
    return field_locator[len(KEY_PREFIX):] if field_locator.startswith(KEY_PREFIX) else None


def _clean(text: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("*", "").strip()[:limit]


def _kind(element: dict[str, Any]) -> str | None:
    tag, kind = (element.get("tag") or "").lower(), (element.get("type") or "text").lower()
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if tag != "input":
        return None
    if kind in ("hidden", "submit", "button", "reset", "image", "password", "search"):
        return None
    return kind if kind in (*TEXT_KINDS, "checkbox", "radio", "file") else "text"


def _role(kind: str, label: str) -> str:
    lowered = label.lower()
    if kind == "file":
        if "cover" in lowered:
            return "cover_letter"
        return "resume" if re.search(r"resume|cv|curriculum", lowered) else "question"
    if kind == "email":
        return "email"
    if kind == "tel":
        return "phone"
    for role, pattern in LABEL_ROLES:
        if pattern.search(lowered):
            return role
    return "question"


def _eeo(label: str) -> bool:
    lowered = label.lower()
    return any(marker in lowered for marker in EEO_MARKERS)


def analyze(url: str, elements: list[dict[str, Any]]) -> ApplicationForm | None:
    """The form the observed elements make up, or None when there is no input to fill."""
    fields: list[FormField] = []
    radios: dict[str, list[dict[str, Any]]] = {}
    submit_label = ""
    for element in elements:
        if element.get("submit_like"):
            submit_label = submit_label or _clean(element.get("label"), 120)
            continue
        kind = _kind(element)
        if kind is None or not element.get("key"):
            continue
        if kind == "radio":
            radios.setdefault(element.get("name") or element["key"], []).append(element)
            continue
        label = _clean(element.get("label")) or _clean(element.get("name")) or element["key"]
        options = [FormOption(label=_clean(o.get("label"), 300) or _clean(o.get("value"), 300),
                              value=_clean(o.get("value"), 300))
                   for o in element.get("options") or [] if _clean(o.get("value"), 300)] if kind == "select" else []
        fields.append(FormField(id=f"bu_{element['key']}"[:200], label=label, kind=kind, role=_role(kind, label),
                                required=bool(element.get("required")), options=options,
                                locator=locator(element["key"]), eeo=_eeo(label),
                                legal=kind == "checkbox" and is_legal(label)))
    for name, group in radios.items():
        label = _clean(group[0].get("group_label")) or _clean(name)
        options = [FormOption(label=_clean(r.get("label"), 300) or _clean(r.get("value"), 300),
                              value=_clean(r.get("value"), 300)) for r in group]
        key = group[0].get("group_key") or group[0]["key"]
        fields.append(FormField(id=f"bu_{key}"[:200], label=label, kind="radio", role="question",
                                required=any(r.get("required") for r in group), options=options,
                                locator=locator(key), eeo=_eeo(label), legal=False))
    if not fields:
        return None
    host = (urlparse(url or "").hostname or "")[:200]
    return with_fingerprint(ApplicationForm(adapter=NAME, host=host, fields=fields,
                                            submit=SubmitControl(present=bool(submit_label), label=submit_label)))
