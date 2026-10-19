"""Phase 5 has no automated final Submit, and this file proves it stays that way.

Four independent guarantees, each tested here:
  1. no code under app/ makes a page receive a click, a key press, a tap, a synthetic event or a mouse/keyboard action;
     the application modules also never evaluate scripts or call submit;
  2. `ApplicationPage`, the only thing application code can do to a page, has no such method;
  3. an execution can never be moved into a submit stage (SUBMITTING, CONFIRMING, CONFIRMED), by `set_stage` or by
     setting the column directly;
  4. the application browser aborts every non-GET request (tests/test_application_browser.py, with real Chromium,
     shows a full ASSIST run over the fixture form makes zero POSTs).
"""
import ast
import inspect
from pathlib import Path

import pytest

from app.infrastructure.application_browser import ApplicationPage
from app.infrastructure.application_browser.ephemeral import _Page
from app.models import ApplicationExecutionTask
from app.services import application_execution
from app.services.ats_forms import SubmitControl, greenhouse

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
# Methods that act on a page as a person would to submit something. Banned everywhere in app/.
INPUT_ACTIONS = {"click", "dblclick", "tap", "press", "press_sequentially", "dispatch_event", "drag_to", "hover",
                 "request_submit", "keyboard", "mouse", "touchscreen"}
# In the application modules, also no script evaluation and nothing named submit.
APPLICATION_ONLY = {"evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all", "add_init_script",
                    "expose_function", "submit", "type"}
APPLICATION_MODULES = ("app/infrastructure/application_browser", "app/services/ats_forms",
                       "app/agents/application.py", "app/services/application_execution.py",
                       "app/services/answer_resolver.py")


def page_actions(source: str, banned: set[str]) -> list[str]:
    """Calls or attribute reads of a banned name (`page.click(...)`, `page.keyboard.press`)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            found.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "requestSubmit" in node.value:
            found.append("requestSubmit")
    return found


def _application_file(path: Path) -> bool:
    relative = str(path.relative_to(ROOT))
    return any(relative == m or relative.startswith(m + "/") for m in APPLICATION_MODULES)


def test_no_production_code_clicks_presses_or_dispatches_events():
    offenders = {}
    for path in sorted(APP.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        banned = INPUT_ACTIONS | (APPLICATION_ONLY if _application_file(path) else set())
        if hits := page_actions(path.read_text(), banned):
            offenders[str(path.relative_to(ROOT))] = sorted(set(hits))
    assert offenders == {}, ("Job Hunt OS does not submit applications (Phase 5). These modules can act on a page "
                             f"the way a submit would: {offenders}")


def test_every_application_module_is_scanned():
    for module in APPLICATION_MODULES:
        assert (ROOT / module).exists(), f"{module} is listed as an application module but does not exist"


def test_the_guard_detects_what_it_bans():
    assert page_actions("await page.locator('#submit_app').click()", INPUT_ACTIONS) == ["click"]
    assert page_actions("await page.keyboard.press('Enter')", INPUT_ACTIONS) == ["press", "keyboard"]
    assert page_actions("await page.evaluate('f.requestSubmit()')", APPLICATION_ONLY) == ["evaluate",
                                                                                          "requestSubmit"]
    assert page_actions("await page.locator('#email').fill('a@b.test')", INPUT_ACTIONS | APPLICATION_ONLY) == []


def test_an_application_page_has_no_way_to_submit():
    public = {name for name, _ in inspect.getmembers(ApplicationPage) if not name.startswith("_")}
    assert public == {"goto", "html", "fill", "select", "check", "upload", "value_of", "is_checked",
                      "blocked_requests"}
    implemented = {name for name in vars(_Page) if not name.startswith("_")}
    assert implemented <= public | {"install"}
    assert not any(word in name for name in public | implemented for word in ("click", "press", "submit", "eval"))


def test_a_form_records_its_submit_control_without_a_locator():
    assert set(SubmitControl.model_fields) == {"present", "label"}
    assert not hasattr(greenhouse, "submit")


@pytest.mark.parametrize("stage", ["SUBMITTING", "CONFIRMING", "CONFIRMED"])
def test_no_execution_can_reach_a_submit_stage(stage):
    task = ApplicationExecutionTask(stage="FILLING")
    with pytest.raises(application_execution.SubmitForbidden):
        application_execution.set_stage(task, stage)
    with pytest.raises(application_execution.SubmitForbidden):
        task.stage = stage  # the column itself refuses it, whoever sets it
    assert task.stage == "FILLING"
    application_execution.set_stage(task, "READY_TO_SUBMIT")  # the last stage Phase 5 can reach
    assert task.stage == "READY_TO_SUBMIT"
