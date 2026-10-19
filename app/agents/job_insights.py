"""The Job Insights graph (Phase 4): one research run for an approved job, through the existing research service.

    LOAD_JOB_CONTEXT → COLLECT_AVAILABLE_SOURCES → NORMALIZE_COMPENSATION → RETRIEVE_COMPANY_FACTS
      → RETRIEVE_INTERVIEW_REPORTS → SYNTHESIZE_INSIGHTS → VALIDATE_PROVENANCE → PERSIST_INSIGHTS

The graph is a conductor over `app.services.job_insights.service`, the module the current path runs as one function
(`service.process`). It runs the same steps on the same task row and claim: `service.load` (snapshot, cache),
`service.collect` (the source adapters: posting, recruiter, official pages, Apify actors; each matched by
`matching.assess`), `service.store` (InsightObservation rows with their provenance), `compensation.compensation_insight`
(the posting's own salary first, a recruiter budget second, third-party figures only as market data, conflicts listed),
`synthesis.run` (the gateway, the existing validators) and `service.settle` (the same sections, the last good synthesis
kept when the model did not run, retry and finish). Research is started only by `workflow.approve_job` →
`job_insights.tasks.on_approved`, never before approval, and approval never waits for it.

What the graph adds:
  * every step is recorded once (`app.agents.effects`): a replayed step never calls the sources or the model twice in
    one attempt. Between steps the run keeps only ids and counts; observations live in their table and the validated
    synthesis on the task row (`job_insight_tasks.staged`), never in a checkpoint (it can hold page text, and the
    snapshot holds the Apify token);
  * VALIDATE_PROVENANCE checks the sections in code before they are saved (`job_insights.provenance`): employer pay
    only from the posting, market figures only from market sources, official values only from the company, culture
    only from employees, reported questions only from reports, generated questions never attributed. An item that
    breaks a rule is dropped, never re-labelled.

SHADOW runs (JOB_INSIGHTS_GRAPH_MODE=shadow) take the deterministic steps only, over a run the current path just
finished: compensation recomputed against what it stored, and the provenance check over the stored sections. They
never call a source or a model and never write.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import effects

GRAPH = "job_insights"
GRAPH_VERSION = "v1"
EXPORT_FIELDS = (
    "job_id", "task_id", "mode", "attempt", "step", "outcome", "code", "insight_id", "reused", "sources_answered",
    "sources_failed", "observations", "comp_status", "employer_pay", "recruiter_pay", "market_pay", "comp_conflict",
    "official_values", "employee_culture", "company_facts", "needs_review", "reported_questions", "interview_process",
    "synthesized", "model_errors", "dropped", "provenance_violations", "stored_comp_status",
)

LOAD = "LOAD_JOB_CONTEXT"
COLLECT = "COLLECT_AVAILABLE_SOURCES"
NORMALIZE = "NORMALIZE_COMPENSATION"
COMPANY = "RETRIEVE_COMPANY_FACTS"
INTERVIEW = "RETRIEVE_INTERVIEW_REPORTS"
SYNTHESIZE = "SYNTHESIZE_INSIGHTS"
VALIDATE = "VALIDATE_PROVENANCE"
PERSIST = "PERSIST_INSIGHTS"
NODES = (LOAD, COLLECT, NORMALIZE, COMPANY, INTERVIEW, SYNTHESIZE, VALIDATE, PERSIST)

LOST, SKIPPED = "lost", "skipped"


class InsightsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: int
    task_id: int
    attempt: int = 1
    mode: Literal["ON", "SHADOW"] = "ON"


class InsightsResume(BaseModel):
    """Never used: research waits for no person and no other worker. Present because every graph names one."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["continue"]


class InsightsState(TypedDict, total=False):
    job_id: int
    task_id: int
    attempt: int
    mode: str
    step: str
    outcome: str
    code: str
    insight_id: int | None
    reused: int
    sources_answered: int
    sources_failed: int
    observations: int
    comp_status: str | None
    employer_pay: int
    recruiter_pay: int
    market_pay: int
    comp_conflict: bool
    official_values: int
    employee_culture: int
    company_facts: int
    needs_review: int
    reported_questions: int
    interview_process: int
    synthesized: bool
    model_errors: int
    dropped: int
    provenance_violations: int
    stored_comp_status: str | None


@dataclass(frozen=True)
class InsightsContext:
    """Per-run context, never checkpointed: the thread lease, a session factory and the research task's claim."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    hold: Callable[[str], None] | None = None
    task_id: int | None = None
    claim: Any = None  # job_insights.tasks.InsightClaim (its lock token guards every write, as on the current path)


def _hold(ctx: InsightsContext, point: str) -> None:
    if ctx.hold is not None:
        ctx.hold(point)


@contextmanager
def _read(ctx: InsightsContext) -> Iterator[Session]:
    with ctx.sessions() as db:
        db.info["user_id"] = ctx.run.user_id
        try:
            yield db
        finally:
            db.rollback()


def _end(step: str, outcome: str, code: str | None = None, **values: Any) -> dict[str, Any]:
    return {"step": step, "outcome": outcome, "code": code or outcome, **values}


def _shadow(state: InsightsState) -> bool:
    return state.get("mode") == "SHADOW"


def _key(state: InsightsState, step: str) -> str:
    return f"insights.{step}:{state['task_id']}:a{state.get('attempt', 1)}"


def _recorded(db: Session, ctx: InsightsContext, key: str) -> Any:
    from app.models import AgentEffect

    return db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == ctx.run.thread_pk,
                                               AgentEffect.effect_key == key))


def _insight(db: Session, job_id: int) -> Any:
    from app.models import JobInsight

    return db.scalar(select(JobInsight).where(JobInsight.job_id == job_id))


def _observations(db: Session, job_id: int) -> list[dict[str, Any]]:
    from app.services.job_insights import service

    insight = _insight(db, job_id)
    return service.observations_of(db, insight.id) if insight is not None else []


def _accepted(observations: list[dict[str, Any]], category: str, *types: str) -> list[dict[str, Any]]:
    return [o for o in observations if o["category"] == category and o["match_status"] == "ACCEPTED"
            and (not types or o["source_type"] in types)]


# ───────────── LOAD_JOB_CONTEXT ─────────────

def load(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """The research snapshot (service.load: the job, its insight row, the posting's salary, the recruiter's notes, the
    cache). Kept out of the checkpoint; only its size is recorded."""
    ctx = runtime.context
    from app.services.job_insights import service

    if _shadow(state):
        with _read(ctx) as db:
            insight = _insight(db, state["job_id"])
            if insight is None:
                return _end(LOAD, SKIPPED, "NO_INSIGHT")
            insight_id = insight.id
        return {"step": LOAD, "insight_id": insight_id}
    loaded = service.load(ctx.sessions, ctx.claim)
    if isinstance(loaded, str):
        return _end(LOAD, loaded, loaded.upper())
    snapshot, reused = loaded
    with _read(ctx) as db:
        insight = _insight(db, snapshot.job_id)
        insight_id = insight.id if insight is not None else None
    return {"step": LOAD, "insight_id": insight_id, "reused": len(reused)}


# ───────────── COLLECT_AVAILABLE_SOURCES ─────────────

def collect(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """Run every configured source (service.collect) and store what answered (service.store), once per attempt: the
    effect record commits with the observations, so a replay finds them and never calls the sources again."""
    ctx = runtime.context
    from app.services import backoff, tracing
    from app.services.job_insights import service
    from app.services.job_insights.base import BLOCKED, FAILED
    from app.services.job_insights.service import ANSWERED

    key = _key(state, "collect")
    with _read(ctx) as db:
        done = _recorded(db, ctx, key)
    if done is None:
        loaded = service.load(ctx.sessions, ctx.claim)
        if isinstance(loaded, str):
            return _end(COLLECT, loaded, loaded.upper())
        snapshot, reused = loaded
        with backoff.bypass(ctx.claim.run_now), tracing.trace("job_insights", job_id=snapshot.job_id,
                                                              user_id=snapshot.user_id):
            results = service.collect(snapshot, reused)
        summary = {"answered": sum(1 for r in results if r.status in ANSWERED),
                   "failed": sum(1 for r in results if r.status in (FAILED, BLOCKED))}
        _hold(ctx, "collect:before_store")
        stored = service.store(ctx.sessions, ctx.claim, results, snapshot.now,
                               within=lambda db: effects.within(db, ctx.run, key, summary))
        if stored is None:
            return _end(COLLECT, LOST, "LEASE_LOST")
    else:
        summary = done.summary or {}
    with _read(ctx) as db:
        count = len(_observations(db, state["job_id"]))
    return {"step": COLLECT, "sources_answered": int(summary.get("answered", 0)),
            "sources_failed": int(summary.get("failed", 0)), "observations": count}


# ───────────── NORMALIZE_COMPENSATION ─────────────

def normalize(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """The compensation picture, computed in code from the stored observations: the employer's own figure, a
    recruiter's budget and market data, kept apart; conflicting estimates are listed, never averaged."""
    ctx = runtime.context
    from app.models import Job
    from app.services import compensation

    with _read(ctx) as db:
        job, insight = db.get(Job, state["job_id"]), _insight(db, state["job_id"])
        if job is None or insight is None:
            return _end(NORMALIZE, SKIPPED, "JOB_DELETED")
        rows = _accepted(_observations(db, job.id), "COMPENSATION")
        comp = compensation.compensation_insight(rows, role_title=job.title or "",
                                                 normalized_title=insight.normalized_title or "",
                                                 company=job.company or "", location=job.location or "")
        stored = (insight.compensation or {}).get("status") if _shadow(state) else None
    return {"step": NORMALIZE, "comp_status": comp.get("status"),
            "employer_pay": sum(1 for r in rows if r["source_type"] in ("EMPLOYER", "JOB_DESCRIPTION")),
            "recruiter_pay": sum(1 for r in rows if r["source_type"] == "RECRUITER"),
            "market_pay": sum(1 for r in rows if r["source_type"] in compensation.MARKET_SOURCES),
            "comp_conflict": comp.get("status") == "CONFLICTING_ESTIMATES", "stored_comp_status": stored}


# ───────────── RETRIEVE_COMPANY_FACTS / RETRIEVE_INTERVIEW_REPORTS ─────────────

def company(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """What the research holds about the company, by kind: official values (the company's own pages), employee-reported
    culture and sentiment, other company facts, and what waits for a person (NEEDS_REVIEW). Counts only."""
    ctx = runtime.context
    with _read(ctx) as db:
        rows = _observations(db, state["job_id"])
    return {"step": COMPANY, "official_values": len(_accepted(rows, "VALUES", "OFFICIAL_COMPANY")),
            "employee_culture": len(_accepted(rows, "CULTURE")) + len(_accepted(rows, "SENTIMENT")),
            "company_facts": len(_accepted(rows, "COMPANY")),
            "needs_review": sum(1 for o in rows if o["match_status"] == "NEEDS_REVIEW")}


def interview(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """Interview reports the research holds: questions candidates reported and process reports. Counts only; these
    are the only sources a REPORTED question may cite."""
    ctx = runtime.context
    with _read(ctx) as db:
        rows = _observations(db, state["job_id"])
    return {"step": INTERVIEW, "reported_questions": len(_accepted(rows, "INTERVIEW_QUESTION")),
            "interview_process": len(_accepted(rows, "INTERVIEW_PROCESS"))}


# ───────────── SYNTHESIZE_INSIGHTS ─────────────

def synthesize(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """The company and interview workers (synthesis.run: the gateway, `untrusted()` research, the validators that
    reject an uncited id). Once per attempt: the validated answer is staged on the task row with the effect record."""
    ctx = runtime.context
    from app.services.job_insights import synthesis, tasks

    key = _key(state, "synthesize")
    with _read(ctx) as db:
        done = _recorded(db, ctx, key)
        observations = _observations(db, state["job_id"])
    if done is None:
        result = synthesis.run(ctx.sessions, user_id=ctx.run.user_id, job_id=state["job_id"],
                               attempt=state.get("attempt", 1), observations=observations)
        summary = {"synthesized": result.company is not None or result.interview is not None,
                   "errors": len(result.errors), "dropped": len(result.dropped)}
        with ctx.sessions() as db:
            task = tasks.held(db, ctx.claim)
            if task is None:
                return _end(SYNTHESIZE, LOST, "LEASE_LOST")
            effects.within(db, ctx.run, key, summary)
            task.staged = synthesis.to_stage(result)
            db.commit()
    else:
        summary = done.summary or {}
    return {"step": SYNTHESIZE, "synthesized": bool(summary.get("synthesized")),
            "model_errors": int(summary.get("errors", 0)), "dropped": int(summary.get("dropped", 0))}


# ───────────── VALIDATE_PROVENANCE ─────────────

def validate(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """Check the sections this run would save (or, in shadow, the sections the current path saved) against their
    sources (job_insights.provenance). Reads only; PERSIST drops what this finds."""
    ctx = runtime.context
    from app.models import Job, JobInsightTask
    from app.services.job_insights import provenance, service, synthesis

    with _read(ctx) as db:
        job, insight = db.get(Job, state["job_id"]), _insight(db, state["job_id"])
        if job is None or insight is None:
            return _end(VALIDATE, SKIPPED, "JOB_DELETED")
        observations = _observations(db, job.id)
        if _shadow(state):
            sections = {name: getattr(insight, name) for name in service.INSIGHT_SECTIONS}
        else:
            task = db.get(JobInsightTask, state["task_id"])
            results = service.results_from_status(task.source_status if task is not None else None)
            result = synthesis.from_stage(task.staged if task is not None else None)
            sections = service.sections_for(db, job, insight, observations, result, results)
        report = provenance.check(sections, observations)
    if _shadow(state):
        same = report.count == 0 and state.get("comp_status") == state.get("stored_comp_status")
        return _end(VALIDATE, "SHADOW", "PARITY" if same else "DIFFERS", provenance_violations=report.count)
    return {"step": VALIDATE, "provenance_violations": report.count}


# ───────────── PERSIST_INSIGHTS ─────────────

def persist(state: InsightsState, runtime: Runtime[InsightsContext]) -> dict[str, Any]:
    """service.settle: the same sections, statuses and retry rules as the current path, minus any item the provenance
    check refused. Once per attempt (the effect record commits with the insight)."""
    ctx = runtime.context
    from app.services.job_insights import provenance, service, synthesis, tasks

    key = _key(state, "persist")
    with ctx.sessions() as db:
        db.info["user_id"] = ctx.run.user_id
        done = _recorded(db, ctx, key)
        if done is not None:
            return _end(PERSIST, str((done.summary or {}).get("label") or "completed"))
        task = tasks.held(db, ctx.claim)
        if task is None:
            return _end(PERSIST, LOST, "LEASE_LOST")
        results = service.results_from_status(task.source_status)
        result = synthesis.from_stage(task.staged)
        observations = _observations(db, state["job_id"])
    def record(db: Session) -> None:
        effects.within(db, ctx.run, key, {"label": "pending"})

    label = service.settle(ctx.sessions, ctx.claim, results, result, observations, service.utcnow(),
                           sections_filter=lambda sections: provenance.enforce(sections, observations)[0],
                           within=record)
    with ctx.sessions() as db:
        from app.models import AgentEffect

        row = db.scalar(select(AgentEffect).where(AgentEffect.agent_thread_id == ctx.run.thread_pk,
                                                  AgentEffect.effect_key == key))
        if row is not None:
            row.summary = {"label": label}
            db.commit()
    return _end(PERSIST, label, label.upper(), model_errors=len(result.errors))


def _next(after: str) -> Callable[[InsightsState], str]:
    def route(state: InsightsState) -> str:
        return END if state.get("outcome") else after
    return route


def _after_interview(state: InsightsState) -> str:
    if state.get("outcome"):
        return END
    return "validate" if _shadow(state) else "synthesize"


def _after_load(state: InsightsState) -> str:
    if state.get("outcome"):
        return END
    return "normalize" if _shadow(state) else "collect"


def build(checkpointer: Any) -> Any:
    graph = StateGraph(InsightsState, context_schema=InsightsContext)
    graph.add_node("load", load)
    graph.add_node("collect", collect)
    graph.add_node("normalize", normalize)
    graph.add_node("company", company)
    graph.add_node("interview", interview)
    graph.add_node("synthesize", synthesize)
    graph.add_node("validate", validate)
    graph.add_node("persist", persist)
    graph.add_edge(START, "load")
    graph.add_conditional_edges("load", _after_load, ["collect", "normalize", END])
    graph.add_conditional_edges("collect", _next("normalize"), ["normalize", END])
    graph.add_conditional_edges("normalize", _next("company"), ["company", END])
    graph.add_conditional_edges("company", _next("interview"), ["interview", END])
    graph.add_conditional_edges("interview", _after_interview, ["synthesize", "validate", END])
    graph.add_conditional_edges("synthesize", _next("validate"), ["validate", END])
    graph.add_conditional_edges("validate", _next("persist"), ["persist", END])
    graph.add_edge("persist", END)
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in EXPORT_FIELDS if k in state and state[k] is not None}
