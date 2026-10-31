"""What may happen on an application page in the browser-use backend, decided by Python (Phase 6).

    fill_deterministic(…)   every RESOLVED field into the element it was resolved for (no model)
    navigate(…)             only where that is not enough (the form must be revealed, a section opened): the
                            BROWSER_NAVIGATOR worker proposes one step at a time and `check` decides each
    check(proposal, …)      → Step (one runner command) or Refusal
    read_back(…)            what the page now holds for each required field, against what was resolved

A proposal is only ever an element key and a reference: `check` requires the element to be in the latest
observation, a fill to use a RESOLVED answer that was resolved for that very element (the form field's locator is
the element's key), a select's value to be one of the element's own options, an upload to be one of the run's own
packet files on a file input of the matching role, and a click to be on a control that cannot submit (and, for a
link, that stays on the application's hosts). Password fields are never filled here. The value always comes from
the AnswerResolver, never from the model. Three refusals end the run (BLOCKED INVALID_ACTION): instructions in a page
cannot talk the browser into anything this function does not allow.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.prompts.contracts import BrowserActionProposal
from app.services import ats_forms
from app.services.ats_forms import ApplicationForm, FormField, generic

MAX_REFUSALS = 3
MAX_ERRORS = 3
REVEAL_STEPS = 8
TEXT_KINDS = ("text", "email", "tel", "url", "number", "textarea")
# Accepting cookies, terms or a consent is the person's decision; declining non-essential cookies is allowed.
CONSENT_WORDS = re.compile(r"^\s*(accept|agree|i agree|allow( all)?|consent|got it|ok(ay)?|continue with all|yes,? i)"
                           r"\b", re.IGNORECASE)


@dataclass(frozen=True)
class Step:
    cmd: str
    args: dict[str, Any]
    field_id: str | None = None


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str


@dataclass
class FillResult:
    outcome: str = "FILLED"  # FILLED | NEEDS_HUMAN | BLOCKED
    code: str | None = None
    filled: set[str] = field(default_factory=set)
    missing: list[str] = field(default_factory=list)
    actions: int = 0
    refusals: int = 0
    model_steps: int = 0
    blocked_requests: int = 0
    refused: list[dict[str, Any]] = field(default_factory=list)
    ask_element: str | None = None  # the element the navigator asked a person about, if it named one


def _elements(observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["key"]: e for e in observation.get("elements") or [] if e.get("key")}


def _group_keys(observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Radio groups are one field: their key is the group's (generic.analyze), any option element stands for it."""
    return {e["group_key"]: e for e in observation.get("elements") or [] if e.get("group_key")}


def element_for(field_: FormField, observation: dict[str, Any]) -> dict[str, Any] | None:
    key = generic.key_of(field_.locator)
    if key is None:
        return None
    found = _elements(observation).get(key) or _group_keys(observation).get(key)
    attached = (observation.get("attached") or {}).get(key)
    if found is None and attached and field_.kind == "file":
        # The board replaced the file input with the file's name once the upload was taken (the runner saw it).
        return {"key": key, "tag": "input", "type": "file", "attached": attached}
    return found


def _compatible(field_: FormField, element: dict[str, Any]) -> bool:
    kind, tag = element.get("type") or "", element.get("tag") or ""
    if kind == "password":
        return False
    if field_.kind == "select":
        return tag == "select"
    if field_.kind == "combobox":
        return tag == "input" and element.get("role") == "combobox"
    if field_.kind in ("checkbox", "radio", "file"):
        return tag == "input" and kind == field_.kind
    return tag == "textarea" if field_.kind == "textarea" else tag == "input" and kind in TEXT_KINDS + ("search",)


def step_for(field_: FormField, element: dict[str, Any], value: Any) -> Step | Refusal | None:
    """The one command that puts `value` into the element resolved for `field_` (None: nothing to do)."""
    key = element["group_key"] if field_.kind == "radio" else element["key"]
    if field_.kind == "file":
        if not isinstance(value, dict) or not value.get("path"):
            return Refusal("NOT_RESOLVED", "No packet file for this input")
        return Step("upload", {"key": element["key"], "path": value["path"]}, field_.id)
    if field_.kind == "select":
        if str(value) not in [o.get("value") for o in element.get("options") or []]:
            return Refusal("NO_SUCH_OPTION", "The resolved answer is not one of this select's options")
        return Step("select", {"key": key, "value": str(value)}, field_.id)
    if field_.kind == "combobox":
        return Step("choose", {"key": key, "text": str(value)}, field_.id)
    if field_.kind == "checkbox":
        return Step("check", {"key": key}, field_.id) if value is True else None
    if field_.kind == "radio":
        return Step("check", {"key": element["key"], "value": str(value)}, field_.id)
    return Step("fill", {"key": key, "text": str(value)}, field_.id)


def check(proposal: BrowserActionProposal, observation: dict[str, Any], form: ApplicationForm,
          plan: dict[str, Any], values: dict[str, Any], *, hosts: set[str]) -> Step | Refusal | str:
    """Decide one proposal. Returns a Step, a Refusal, or "done" / "scroll" / "ask_human"."""
    if proposal.action in ("done", "ask_human"):
        return proposal.action
    if proposal.action == "scroll":
        return Step("scroll", {"direction": "down"})
    elements = _elements(observation)
    element = elements.get(proposal.element_key or "") or _group_keys(observation).get(proposal.element_key or "")
    if element is None:
        return Refusal("NO_SUCH_ELEMENT", "The element is not on the page as last observed")
    if element.get("type") == "password":
        return Refusal("PASSWORD_FIELD", "Password fields are never filled by a navigation step")
    if proposal.action == "click_control":
        if element.get("submit_like"):
            return Refusal("SUBMIT_REFUSED", "This control could submit the form")
        if element.get("tag") not in ("a", "button") and element.get("role") not in ("button", "tab", "link"):
            return Refusal("NOT_A_CONTROL", "Only buttons and links can be clicked")
        if element.get("tag") == "a" and element.get("href_host") and element["href_host"] not in hosts:
            return Refusal("OFF_ROUTE", "The link leaves the application's own hosts")
        if CONSENT_WORDS.match(element.get("label") or ""):
            return Refusal("CONSENT_REFUSED", "Accepting cookies, terms or a consent is the person's decision")
        return Step("click", {"key": element["key"]})
    fields = {f.id: f for f in form.fields}
    field_ = fields.get(proposal.answer_ref or "")
    if field_ is None:
        return Refusal("NO_SUCH_FIELD", "The answer reference is not a field of this form")
    entry = (plan.get("fields") or {}).get(field_.id) or {}
    if entry.get("status") != "RESOLVED" or field_.id not in values:
        return Refusal("NOT_RESOLVED", "Only an answer resolved from a verified source can be filled")
    resolved_for = generic.key_of(field_.locator)
    if resolved_for is None or resolved_for not in (element.get("key"), element.get("group_key")):
        return Refusal("WRONG_ELEMENT", "An answer goes only into the element it was resolved for")
    if not _compatible(field_, element):
        return Refusal("WRONG_KIND", "The element cannot hold this kind of answer")
    expected = {"fill_field": TEXT_KINDS, "select_option": ("select", "combobox"), "check_box": ("checkbox", "radio"),
                "upload_file": ("file",)}[proposal.action]
    if field_.kind not in expected:
        return Refusal("WRONG_ACTION", "This action does not fit the field")
    if proposal.action == "upload_file" and proposal.file_ref != ("cover_letter" if field_.role == "cover_letter"
                                                                 else "resume"):
        return Refusal("WRONG_FILE", "The file does not match the input's role")
    return step_for(field_, element, values[field_.id]) or "done"


def field_refs(form: ApplicationForm, plan: dict[str, Any], filled: set[str]) -> list[dict[str, Any]]:
    """FIELDS for the navigator: references and statuses, never a value."""
    out = []
    for f in form.fields:
        status = ((plan.get("fields") or {}).get(f.id) or {}).get("status")
        out.append({"ref": f.id, "label": f.label[:200], "kind": f.kind, "required": f.required, "status": status,
                    "filled": f.id in filled})
    return out


Run = Callable[[Coroutine[Any, Any, Any]], Any]


def fill_deterministic(session: Any, run: Run, form: ApplicationForm, plan: dict[str, Any], values: dict[str, Any],
                       observation: dict[str, Any], result: FillResult, max_actions: int) -> None:
    """Every RESOLVED field into its own element, without a model. Fields whose element is not on the page are left
    for `navigate`."""
    from app.infrastructure.application_browser.browser_use_sidecar import SidecarRefused

    for field_ in form.fields:
        if field_.id not in values or field_.id in result.filled or result.actions >= max_actions:
            continue
        element = element_for(field_, observation)
        if element is None or not _compatible(field_, element):
            continue
        if element.get("attached"):  # already uploaded in this window: the same file, or the person's own choice
            result.filled.add(field_.id)
            continue
        step = step_for(field_, element, values[field_.id])
        if step is None:
            result.filled.add(field_.id)
            continue
        if isinstance(step, Refusal):
            result.refused.append({"field": field_.id, "code": step.code})
            continue
        try:
            run(session.call(step.cmd, **step.args))
            result.filled.add(field_.id)
        except SidecarRefused as exc:
            result.refused.append({"field": field_.id, "code": exc.code})
        result.actions += 1


def navigate(session: Any, run: Run, propose: Callable[[Any], BrowserActionProposal], build_prompt: Callable[..., Any],
             form: ApplicationForm | None, plan: dict[str, Any], values: dict[str, Any], result: FillResult, *,
             hosts: set[str], max_actions: int, reveal_only: bool = False) -> dict[str, Any]:
    """The model-guided loop for what the deterministic pass could not reach. Returns the last observation."""
    from app.infrastructure.application_browser.browser_use_sidecar import SidecarRefused

    history: list[dict[str, Any]] = []
    errors = 0
    observation = run(session.call("observe", html=True))
    steps = REVEAL_STEPS if reveal_only else max_actions
    empty = ApplicationForm(adapter=generic.NAME, host="", fields=[], submit=ats_forms.SubmitControl(present=False))
    for number in range(1, steps + 1):
        problem = ats_forms.page_problem(observation.get("html") or "", title=observation.get("title"),
                                     captcha=observation.get("captcha"))
        if problem:
            result.outcome, result.code = ("NEEDS_HUMAN" if problem in ats_forms.HUMAN_BLOCKERS else "BLOCKED"), \
                problem
            return observation
        if result.actions >= max_actions:
            break
        current = form or empty
        files = sorted({("cover_letter" if f.role == "cover_letter" else "resume") for f in current.fields
                        if f.kind == "file" and isinstance(values.get(f.id), dict)})
        proposal = propose(build_prompt(fields=field_refs(current, plan, result.filled), files=files,
                                        elements=observation.get("elements") or [],
                                        page_text=observation.get("text") or "", history=history, step=number,
                                        max_steps=steps))
        result.model_steps += 1
        decided = check(proposal, observation, current, plan, values, hosts=hosts)
        if reveal_only and isinstance(decided, Step) and decided.cmd not in ("click", "scroll"):
            decided = "done"  # revealing ends as soon as the model sees a form to fill
        if decided == "done":
            break
        if decided == "ask_human":
            result.outcome, result.code = "NEEDS_HUMAN", f"NAVIGATOR_{proposal.human_reason or 'OTHER'}"
            if proposal.element_key and proposal.element_key in _elements(observation):
                result.ask_element = proposal.element_key
            return observation
        if isinstance(decided, Refusal):
            result.refusals += 1
            result.refused.append({"action": proposal.action, "code": decided.code})
            history.append({"action": proposal.action, "element_key": proposal.element_key,
                            "answer_ref": proposal.answer_ref, "outcome": f"REFUSED {decided.code}"})
            if result.refusals >= MAX_REFUSALS:
                result.outcome, result.code = "BLOCKED", "INVALID_ACTION"
                return observation
            continue
        try:
            run(session.call(decided.cmd, **decided.args))
            outcome = "OK"
            if decided.field_id:
                result.filled.add(decided.field_id)
        except SidecarRefused as exc:
            errors += 1
            outcome = f"ERROR {exc.code}"
            if errors >= MAX_ERRORS:
                result.outcome, result.code = "BLOCKED", exc.code
                return observation
        result.actions += 1
        history.append({"action": proposal.action, "element_key": proposal.element_key,
                        "answer_ref": proposal.answer_ref, "outcome": outcome})
        observation = run(session.call("observe", html=True))
    return observation


def read_required(session: Any, run: Run, form: ApplicationForm, values: dict[str, Any],
                  observation: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Each required field, read back: (missing, person_filled).

    A field with a verified answer must hold exactly that answer. A required field without one passes only when it
    holds something a person entered in the window (a takeover): its id goes to `person_filled`; what it holds is
    never returned. Anything else, or a field not on the page, is missing."""
    from app.infrastructure.application_browser.browser_use_sidecar import SidecarRefused

    missing, person = [], []
    for field_ in form.required_fields():
        element = element_for(field_, observation)
        if element is None:
            missing.append(field_.id)
            continue
        try:
            read = run(session.call("read", key=element["key"]))
        except SidecarRefused:
            missing.append(field_.id)
            continue
        if field_.id in values:
            if not holds(field_, values[field_.id], read):
                missing.append(field_.id)
        elif read.get("filled"):
            person.append(field_.id)
        else:
            missing.append(field_.id)
    return missing, person


def read_back(session: Any, run: Run, form: ApplicationForm, values: dict[str, Any],
              observation: dict[str, Any]) -> list[str]:
    """Required fields that do not hold what they should (see read_required)."""
    return read_required(session, run, form, values, observation)[0]


def holds(field_: FormField, value: Any, read: dict[str, Any]) -> bool:
    if field_.kind == "file":
        return isinstance(value, dict) and Path(value.get("path", "")).name in (read.get("files") or [])
    if field_.kind == "checkbox":
        return bool(read.get("checked")) == (value is True)
    if field_.kind == "combobox":
        return " ".join(str(read.get("value") or "").split()).lower() == " ".join(str(value).split()).lower()
    return str(read.get("value") or "").strip() == str(value).strip()
