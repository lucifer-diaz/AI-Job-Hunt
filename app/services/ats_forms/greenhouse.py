"""Greenhouse hosted application forms (boards.greenhouse.io / job-boards.greenhouse.io and embeds of them).

Reads the `#application_form` form: the standard fields (first and last name, email, phone, resume, cover letter,
LinkedIn and website), the employer's custom questions (`job_application[answers_attributes][n][...]` inputs, selects
and checkboxes) and the voluntary self-identification block (EEO, recorded and never answered). Fills a field with
`ApplicationPage` (app.infrastructure.application_browser), which has no way to click; this module has no submit.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from app.services.ats_forms import (
    ApplicationForm,
    FormField,
    FormOption,
    SubmitControl,
    is_legal,
    with_fingerprint,
)

NAME = "greenhouse"
HOSTS = ("greenhouse.io",)
# The classic hosted form (boards.greenhouse.io and embeds) and the newer job-boards.greenhouse.io one.
FORM_SELECTOR = "#application_form, form#application-form, form.application--form"
EEO_MARKERS = ("eeoc", "demographic", "gender", "race", "ethnicity", "veteran", "disability", "hispanic")
ROLE_PATTERNS = (
    ("first_name", re.compile(r"(^|[\[_])first_name\]?$")),
    ("last_name", re.compile(r"(^|[\[_])last_name\]?$")),
    ("email", re.compile(r"(^|[\[_])email\]?$")),
    ("phone", re.compile(r"(^|[\[_])phone\]?$")),
    ("resume", re.compile(r"(^|[\[_])resume(_text)?\]?$")),
    ("cover_letter", re.compile(r"(^|[\[_])cover_letter(_text)?\]?$")),
    ("location", re.compile(r"(^|[\[_])location\]?$")),
)
LABEL_ROLES = (("linkedin", "linkedin"), ("github", "github"), ("website", "website"), ("portfolio", "website"))


def detect(url: str, html: str) -> bool:
    """A Greenhouse form: served from a Greenhouse host, or an `#application_form` whose inputs use Greenhouse's
    `job_application[...]` names (an embed on the employer's own site)."""
    host = (urlparse(url or "").hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in HOSTS):
        return True
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.select_one("#application_form")
    return form is not None and form.find(attrs={"name": re.compile(r"^job_application\[")}) is not None


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def _label(soup: BeautifulSoup, element: Tag) -> tuple[str, bool]:
    """The field's label text (without its required asterisk) and whether the label marks it required."""
    label = soup.find("label", attrs={"for": element.get("id")}) if element.get("id") else None
    if label is None:
        label = element.find_parent("label")
    if label is None:
        container = element.find_parent(class_=re.compile(r"field")) or element.find_parent("div")
        label = container.find("label") if container is not None else None
    raw = _text(label) or element.get("aria-label") or element.get("placeholder") or element.get("name") or ""
    starred = "*" in raw
    return raw.replace("*", "").strip()[:500], starred


def _locator(element: Tag) -> str:
    if element.get("id") and re.fullmatch(r"[A-Za-z][\w-]*", element["id"]):
        return f"#{element['id']}"
    name = (element.get("name") or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'{element.name}[name="{name}"]'


def _role(element: Tag, label: str) -> str:
    key = element.get("id") or element.get("name") or ""
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(key):
            return role
    lowered = label.lower()
    for marker, role in LABEL_ROLES:
        if marker in lowered:
            return role
    return "question"


def _is_eeo(element: Tag, label: str) -> bool:
    container = " ".join(filter(None, [element.get("name") or "", element.get("id") or "", label.lower()]))
    block = element.find_parent(id=re.compile(r"eeo|demographic", re.IGNORECASE))
    return block is not None or any(marker in container.lower() for marker in EEO_MARKERS)


def analyze(url: str, html: str) -> ApplicationForm | None:
    """The form's structure, or None when the page has no Greenhouse application form."""
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.select_one(FORM_SELECTOR)
    if form is None:
        return None
    fields: list[FormField] = []
    radios: dict[str, list[Tag]] = {}
    for element in form.find_all(["input", "select", "textarea"]):
        kind = (element.get("type") or "text").lower() if element.name == "input" else element.name
        if kind in ("hidden", "submit", "button", "reset", "image", "password"):
            continue
        if kind == "radio":
            radios.setdefault(element.get("name") or "", []).append(element)
            continue
        if kind not in ("text", "email", "tel", "url", "number", "textarea", "select", "checkbox", "file"):
            kind = "text"
        label, starred = _label(soup, element)
        required = starred or element.has_attr("required") or element.get("aria-required") == "true"
        options = []
        if kind == "select":
            options = [FormOption(label=_text(o)[:300] or o.get("value", "")[:300], value=o.get("value", "")[:300])
                       for o in element.find_all("option") if (o.get("value") or "").strip()]
        field_id = (element.get("id") or element.get("name") or f"field_{len(fields)}")[:200]
        fields.append(FormField(id=field_id, label=label or field_id, kind=kind, role=_role(element, label),
                                required=bool(required), options=options, locator=_locator(element),
                                eeo=_is_eeo(element, label), legal=kind == "checkbox" and is_legal(label)))
    for name, group in radios.items():
        first = group[0]
        legend = first.find_parent("fieldset")
        label = _text(legend.find("legend")) if legend is not None and legend.find("legend") else name
        required = "*" in label or any(r.has_attr("required") or r.get("aria-required") == "true" for r in group)
        options = [FormOption(label=(_label(soup, r)[0] or r.get("value", ""))[:300], value=r.get("value", "")[:300])
                   for r in group]
        safe = name.replace("\\", "\\\\").replace('"', '\\"')
        fields.append(FormField(id=name[:200], label=label.replace("*", "").strip()[:500] or name, kind="radio",
                                role="question", required=bool(required), options=options,
                                locator=f'input[type="radio"][name="{safe}"]', eeo=_is_eeo(first, label)))
    submit = form.find(id="submit_app") or form.find(["button", "input"], attrs={"type": "submit"})
    if submit is None:  # the newer board: a plain button labelled Submit inside the form
        submit = next((b for b in form.find_all("button") if _text(b).lower().startswith("submit")), None)
    label = (submit.get("value") or _text(submit)) if submit is not None else ""
    host = (urlparse(url or "").hostname or "")[:200]
    return with_fingerprint(ApplicationForm(adapter=NAME, host=host, fields=fields,
                                            submit=SubmitControl(present=submit is not None, label=label[:120])))


async def fill(page: Any, form: ApplicationForm, values: dict[str, Any]) -> dict[str, str]:
    """Put each resolved value into its field. `values` maps a field id to text, an option value, a boolean (for a
    checkbox) or {"path": ...} for a file. Returns field id → "filled" or the reason it was not. Never submits."""
    report: dict[str, str] = {}
    for field in form.fields:
        if field.id not in values:
            continue
        value = values[field.id]
        try:
            if field.kind == "file":
                await page.upload(field.locator, value["path"])
            elif field.kind == "select":
                await page.select(field.locator, str(value))
            elif field.kind == "radio":
                safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
                await page.check(f'{field.locator}[value="{safe}"]')
            elif field.kind == "checkbox":
                if value is True:
                    await page.check(field.locator)
            else:
                await page.fill(field.locator, str(value))
            report[field.id] = "filled"
        except Exception as exc:  # noqa: BLE001  one field failing is recorded; validation decides the outcome
            report[field.id] = f"not filled ({type(exc).__name__})"
    return report
