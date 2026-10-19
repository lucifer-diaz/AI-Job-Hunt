"""The application execution graph (Phase 5): one approved job from its packet to a filled, validated form — and no
further. Nothing in it can submit.

    VERIFY_PACKET → ANALYZE_FORM (route + form) → RESOLVE_ANSWERS ─┬→ wait (NEEDS_INPUT: a person answers or confirms)
                                                                  └→ FILL (ASSIST) → VALIDATE → READY_TO_SUBMIT
                                                                     OBSERVE ends at PRE_SUBMIT_VALIDATION

The graph conducts the shared services: `apply_queue.not_ready_reason` and `approval.verify` decide whether the
packet may be used (and FILL checks it again against the digest captured at enqueue), `ats_forms` reads the form,
`answer_resolver` decides each answer's source (never a model), and `apply_queue.record_blocker` records a login wall,
challenge or closed posting where MCP and the Apply kit see it. Browser steps go through the application browser
(`app.infrastructure.application_browser`): an ephemeral page per step that cannot click and aborts every non-GET
request, so a replayed step has no effect outside the process.

Rules a node keeps:
  * Writes happen only while the run holds its thread: `effects.within` locks the thread row and records the step in
    the same transaction as the task's stage. A stopped run raises LeaseLost and writes nothing.
  * Stages move only through `application_execution.set_stage`, which refuses SUBMITTING, CONFIRMING and CONFIRMED.
  * State is ids, enums, counts and hashes only; never page HTML, a form value or an answer.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.agents import effects

GRAPH = "application"
GRAPH_VERSION = "v1"
EXPORT_FIELDS = ("job_id", "task_id", "mode", "step", "outcome", "code", "waiting_for", "route", "field_count",
                 "required_count", "resolved_count", "needs_input_count", "needs_confirmation_count", "filled_count",
                 "missing_required_count", "blocked_requests", "form_fingerprint")

VERIFY_PACKET = "VERIFY_PACKET"
ANALYZE_FORM = "ANALYZE_FORM"
RESOLVE_ANSWERS = "RESOLVE_ANSWERS"
FILL = "FILL"
VALIDATE = "PRE_SUBMIT_VALIDATION"
NODES = (VERIFY_PACKET, ANALYZE_FORM, RESOLVE_ANSWERS, FILL, VALIDATE)

# How a run ends (ApplicationExecutionTask.outcome).
READY, OBSERVED, NEEDS_HUMAN, BLOCKED, UNSUPPORTED = "READY_TO_SUBMIT", "OBSERVED", "NEEDS_HUMAN", "BLOCKED", \
    "UNSUPPORTED"
INPUT = "INPUT"


class ApplicationInput(BaseModel):
    """What the worker starts a run with. Built on the server from the task row; never taken from a caller."""

    model_config = ConfigDict(extra="forbid")
    job_id: int
    task_id: int
    mode: Literal["OBSERVE", "ASSIST"] = "OBSERVE"


class ApplicationResume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["continue"]


class ApplicationState(TypedDict, total=False):
    job_id: int
    task_id: int
    mode: str
    step: str
    outcome: str
    code: str
    waiting_for: str | None
    resume_at: str | None
    route: str | None
    field_count: int
    required_count: int
    resolved_count: int
    needs_input_count: int
    needs_confirmation_count: int
    filled_count: int
    missing_required_count: int
    blocked_requests: int
    form_fingerprint: str | None


@dataclass(frozen=True)
class ApplicationContext:
    """Per-run context, never checkpointed: the lease, a session factory, the application browser and the bridge
    that runs its coroutines on the event loop the browser lives on."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    hold: Callable[[str], None] | None = None
    task_id: int | None = None
    browser: Any = None  # ApplicationSessionManager
    run_async: Callable[[Coroutine[Any, Any, Any]], Any] | None = None


def _hold(ctx: ApplicationContext, point: str) -> None:
    if ctx.hold is not None:
        ctx.hold(point)


def _end(step: str, outcome: str, code: str | None = None, **values: Any) -> dict[str, Any]:
    return {"step": step, "outcome": outcome, "code": code or outcome, "waiting_for": None, "resume_at": None,
            **values}


def _session(ctx: ApplicationContext) -> Session:
    db = ctx.sessions()
    db.info["user_id"] = ctx.run.user_id
    return db


def _load(db: Session, state: ApplicationState) -> tuple[Any, Any]:
    from app.models import ApplicationExecutionTask, Job

    task = db.get(ApplicationExecutionTask, state["task_id"], populate_existing=True)
    job = db.get(Job, state["job_id"])
    return task, job


def _record(db: Session, ctx: ApplicationContext, key: str, task: Any, stage: str, **fields: Any) -> bool:
    """Record this step once (lease checked) and write the task's stage and fields with it. Commits."""
    from app.services import application_execution as service

    _, created = effects.within(db, ctx.run, key)
    if created or task.stage != stage:
        service.set_stage(task, stage)
    for name, value in fields.items():
        setattr(task, name, value)
    db.commit()
    return created


def _key(state: ApplicationState, step: str, extra: str = "") -> str:
    return f"{step.lower()}:{state['task_id']}{extra}"


# ───────────── VERIFY_PACKET ─────────────

def _packet_problem(db: Session, task: Any, job: Any) -> tuple[str, str] | None:
    """(code, reason) when this packet may not be used now: not ready (the shared rules), or not the approved packet
    the execution was queued with."""
    from app.services import apply_queue, approval

    if job is None or task is None:
        return "REMOVED", "The job no longer exists"
    reason = apply_queue.not_ready_reason(db, job)
    checked = approval.verify(db, job.application)
    if not checked.ok:
        return "PACKET_CHANGED", checked.reason or "The approved packet changed"
    if (checked.packet.digest if checked.packet else None) != task.packet_digest:
        return "PACKET_CHANGED", "The packet was approved again after this execution was queued; queue it again"
    if reason is not None:
        return "NOT_READY", reason
    return None


def verify_packet(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)
        if problem:
            if task is not None:
                _record(db, ctx, _key(state, VERIFY_PACKET), task, "BLOCKED", result_code=problem[0],
                        last_error=problem[1][:300])
            return _end(VERIFY_PACKET, BLOCKED, problem[0])
        _record(db, ctx, _key(state, VERIFY_PACKET), task, "DISCOVERING")
    return {"step": VERIFY_PACKET}


# ───────────── ANALYZE_FORM ─────────────

async def _read_form(browser: Any, user_id: int, job_id: int, url: str) -> dict[str, Any]:
    from app.services import ats_forms

    async with browser.session(user_id=user_id, ats="unknown", job_id=job_id) as page:
        load = await page.goto(url)
        html = await page.html()
    problem = ats_forms.page_problem(html, status=load.status, title=load.title)
    if problem:
        return {"problem": problem, "final_url": load.final_url}
    adapter = ats_forms.adapter_for(load.final_url, html)
    if adapter is None:
        return {"problem": ats_forms.UNSUPPORTED, "final_url": load.final_url}
    form = adapter.analyze(load.final_url, html)
    if form is None or not form.fields:
        return {"problem": ats_forms.NO_FORM, "final_url": load.final_url, "route": adapter.NAME}
    return {"form": form.model_dump(), "route": adapter.NAME, "final_url": load.final_url}


BLOCKER_FOR = {"LOGIN_REQUIRED": "login_required", "OTP_REQUIRED": "login_required",
               "CAPTCHA_OR_ANTI_BOT": "captcha", "POSTING_CLOSED": "posting_closed"}


def analyze_form(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    from app.services import apply_queue, ats_forms

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        if task.form and task.form_fingerprint:
            return {"step": ANALYZE_FORM, "route": task.route, "form_fingerprint": task.form_fingerprint,
                    "field_count": len(task.form.get("fields") or [])}
        url = apply_queue.apply_url(job)
        user_id, job_id = job.user_id, job.id
    _hold(ctx, "before_browser")
    read = ctx.run_async(_read_form(ctx.browser, user_id, job_id, url))
    _hold(ctx, "after_browser")
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = read.get("problem")
        if problem in ats_forms.HUMAN_BLOCKERS or problem == ats_forms.POSTING_CLOSED:
            stage = "NEEDS_HUMAN" if problem in ats_forms.HUMAN_BLOCKERS else "BLOCKED"
            created = _record(db, ctx, _key(state, ANALYZE_FORM), task, stage, result_code=problem,
                              route=read.get("route"), interrupt={"kind": stage, "reason": problem})
            if created and job is not None and job.application is not None and job.application.status == "approved":
                apply_queue.record_blocker(db, job, BLOCKER_FOR[problem], recorded_by="application_worker",
                                           detail=f"The application worker stopped at the form: {problem}")
            return _end(ANALYZE_FORM, NEEDS_HUMAN if stage == "NEEDS_HUMAN" else BLOCKED, problem)
        if problem:
            _record(db, ctx, _key(state, ANALYZE_FORM), task, "BLOCKED", result_code=problem,
                    route=read.get("route"))
            return _end(ANALYZE_FORM, UNSUPPORTED, problem, route=read.get("route"))
        form = ats_forms.ApplicationForm.model_validate(read["form"])
        _record(db, ctx, _key(state, ANALYZE_FORM), task, "FORM_ANALYZING", route=read["route"],
                form=form.model_dump(), form_fingerprint=form.fingerprint)
    return {"step": ANALYZE_FORM, "route": read["route"], "form_fingerprint": form.fingerprint,
            "field_count": len(form.fields), "required_count": len(form.required_fields())}


# ───────────── RESOLVE_ANSWERS ─────────────

def resolve_answers(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    from app.services import answer_resolver, ats_forms

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        effects.held(db, ctx.run)
        form = ats_forms.ApplicationForm.model_validate(task.form)
        plan = answer_resolver.plan(db, job, form, confirmed=list(task.confirmed_question_ids or []))
        task, job = _load(db, state)
        fields = plan["fields"]
        counts = {"resolved_count": sum(1 for f in fields.values() if f["status"] == answer_resolver.RESOLVED),
                  "needs_input_count": len(plan["needs_input"]),
                  "needs_confirmation_count": len(plan["needs_confirmation"])}
        waiting = plan["needs_input"] or plan["needs_confirmation"]
        attempt = f":a{task.attempt_count}:{len(plan['needs_input'])}:{len(plan['needs_confirmation'])}"
        if waiting:
            _record(db, ctx, _key(state, RESOLVE_ANSWERS, attempt), task, "NEEDS_INPUT", plan=plan,
                    interrupt={"kind": "NEEDS_INPUT", "question_ids": plan["needs_input"],
                               "confirm_question_ids": plan["needs_confirmation"]})
            return {"step": RESOLVE_ANSWERS, "waiting_for": INPUT, "resume_at": "resolve_answers", **counts}
        _record(db, ctx, _key(state, RESOLVE_ANSWERS, attempt), task, "ANSWERING", plan=plan, interrupt=None)
    return {"step": RESOLVE_ANSWERS, "waiting_for": None, "resume_at": None, **counts}


def _after_resolve(state: ApplicationState) -> str:
    if state.get("outcome"):
        return END
    if state.get("waiting_for"):
        return "wait"
    return "fill" if state.get("mode") == "ASSIST" else "validate"


# ───────────── FILL (ASSIST only) ─────────────

async def _fill_form(browser: Any, user_id: int, job_id: int, url: str, form: Any, values: dict[str, Any],
                     expected: str) -> dict[str, Any]:
    from app.services import ats_forms

    adapter = ats_forms.adapter_named(form.adapter)
    async with browser.session(user_id=user_id, ats=form.adapter, job_id=job_id) as page:
        load = await page.goto(url)
        html = await page.html()
        problem = ats_forms.page_problem(html, status=load.status, title=load.title)
        if problem:
            return {"problem": problem}
        current = adapter.analyze(load.final_url, html)
        if current is None or current.fingerprint != expected:
            return {"problem": "FORM_CHANGED"}
        report = await adapter.fill(page, current, values)
        missing = []
        for field in current.required_fields():
            if field.kind in ("checkbox",):
                ok = await page.is_checked(field.locator)
            elif field.kind == "radio":
                value = values.get(field.id)
                ok = value is not None and await page.is_checked(f'{field.locator}[value="{value}"]')
            else:
                ok = bool((await page.value_of(field.locator)).strip())
            if not ok:
                missing.append(field.id)
        return {"report": report, "missing": missing, "blocked_requests": page.blocked_requests}


def fill(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    from app.services import answer_resolver, apply_queue, approval, ats_forms

    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)  # the approved digest, checked again right before the form
        if problem:
            _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}"), task, "BLOCKED", result_code=problem[0],
                    last_error=problem[1][:300])
            return _end(FILL, BLOCKED, problem[0])
        form = ats_forms.ApplicationForm.model_validate(task.form)
        variant = apply_queue.approved_resume(db, job.application)
        files = {f["format"]: f["path"] for f in apply_queue.packet_files(db, job, variant, ["pdf"])}
        digests = {f["format"]: f["sha256"] for f in apply_queue.packet_files(db, job, variant, ["pdf"])}
        letter = approval.packet_content(variant)["cover_letter"]
        if letter:
            from pathlib import Path

            files["cover_letter"] = answer_resolver.write_cover_letter(Path(files["pdf"]).parent, letter)
        values = answer_resolver.values(db, job, form, task.plan or {}, files=files)
        url, user_id, job_id, expected = apply_queue.apply_url(job), job.user_id, job.id, task.form_fingerprint
        from app.services import application_execution as service

        service.attempt_note(db, task, file_digests=digests)
        _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}"), task, "FILLING")
    _hold(ctx, "before_fill")
    result = ctx.run_async(_fill_form(ctx.browser, user_id, job_id, url, form, values, expected))
    values.clear()  # the values never leave this step
    with _session(ctx) as db:
        task, job = _load(db, state)
        if result.get("problem"):
            code = result["problem"]
            stage = "NEEDS_HUMAN" if code in ats_forms.HUMAN_BLOCKERS else "BLOCKED"
            _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}:done"), task, stage, result_code=code,
                    form=None if code == "FORM_CHANGED" else task.form,
                    form_fingerprint=None if code == "FORM_CHANGED" else task.form_fingerprint)
            return _end(FILL, NEEDS_HUMAN if stage == "NEEDS_HUMAN" else BLOCKED, code)
        filled = sum(1 for v in result["report"].values() if v == "filled")
        _record(db, ctx, _key(state, FILL, f":a{task.attempt_count}:done"), task, "PRE_SUBMIT_VALIDATION")
    return {"step": FILL, "filled_count": filled, "missing_required_count": len(result["missing"]),
            "blocked_requests": result["blocked_requests"]}


# ───────────── PRE_SUBMIT_VALIDATION ─────────────

def validate(state: ApplicationState, runtime: Runtime[ApplicationContext]) -> dict[str, Any]:
    """OBSERVE: every required field has a source. ASSIST: and every required field holds a value on the page. The
    run ends here: READY_TO_SUBMIT is a person's cue to submit (through the Apply kit, which takes the claim)."""
    ctx = runtime.context
    with _session(ctx) as db:
        task, job = _load(db, state)
        problem = _packet_problem(db, task, job)
        if problem:
            _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "BLOCKED",
                    result_code=problem[0], last_error=problem[1][:300])
            return _end(VALIDATE, BLOCKED, problem[0])
        unresolved = [fid for fid, f in ((task.plan or {}).get("fields") or {}).items()
                      if f.get("required") and f.get("status") != "RESOLVED"]
        if unresolved or state.get("missing_required_count"):
            _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "BLOCKED",
                    result_code="REQUIRED_FIELDS_MISSING")
            return _end(VALIDATE, BLOCKED, "REQUIRED_FIELDS_MISSING")
        if state.get("mode") != "ASSIST":
            _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "PRE_SUBMIT_VALIDATION",
                    result_code=OBSERVED)
            return _end(VALIDATE, OBSERVED)
        _record(db, ctx, _key(state, VALIDATE, f":a{task.attempt_count}"), task, "READY_TO_SUBMIT",
                result_code=READY)
    return _end(VALIDATE, READY)


# ───────────── waiting ─────────────

def wait(state: ApplicationState) -> dict[str, Any]:
    """Settle the run until a person answers or confirms what it needs; the worker resumes it then."""
    answer = interrupt({"wait": state.get("waiting_for"), "step": state.get("step"), "job_id": state.get("job_id")})
    ApplicationResume.model_validate(answer if isinstance(answer, dict) else {"action": answer})
    return {"waiting_for": None}


def _next(after: str) -> Callable[[ApplicationState], str]:
    def route(state: ApplicationState) -> str:
        return END if state.get("outcome") else after
    return route


def _after_wait(state: ApplicationState) -> str:
    return state.get("resume_at") or "resolve_answers"


def build(checkpointer: Any) -> Any:
    graph = StateGraph(ApplicationState, context_schema=ApplicationContext)
    graph.add_node("verify_packet", verify_packet)
    graph.add_node("analyze_form", analyze_form)
    graph.add_node("resolve_answers", resolve_answers)
    graph.add_node("fill", fill)
    graph.add_node("validate", validate)
    graph.add_node("wait", wait)
    graph.add_edge(START, "verify_packet")
    graph.add_conditional_edges("verify_packet", _next("analyze_form"), ["analyze_form", END])
    graph.add_conditional_edges("analyze_form", _next("resolve_answers"), ["resolve_answers", END])
    graph.add_conditional_edges("resolve_answers", _after_resolve, ["wait", "fill", "validate", END])
    graph.add_conditional_edges("wait", _after_wait, ["resolve_answers"])
    graph.add_conditional_edges("fill", _next("validate"), ["validate", END])
    graph.add_edge("validate", END)
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in EXPORT_FIELDS if k in state and state[k] is not None}
