"""The resume structuring graph: one AI structuring of an uploaded resume version, through app.services.resume_parsing.

    LOAD_RESUME → EXTRACT_STRUCTURE → GROUND_IN_TEXT → CHECK_QUALITY → APPLY_RESULT ─┬─ filled   (nobody reviewed it)
         │               │                                  │                       └─ compare  (a person did)
         └─ gone / not configured           not configured  └─ kept (poorer than the regex parse)

The graph is a conductor over the service's steps, on the same task row and claim the plain path (`process`) uses:
  * EXTRACT_STRUCTURE is the one model call. It goes through `GatewayChatModel`, bound to the registered
    `resume_parser` prompt, so routing, failover, the contract's correction retry and telemetry are the gateway's.
  * GROUND_IN_TEXT and CHECK_QUALITY are deterministic: every value is held to the resume's own text
    (`resume_parsing.ground`), and a result poorer than the regex parse of the same text is not used.
  * APPLY_RESULT fills the structured data only while nobody has reviewed or edited it; otherwise it stores the
    usual comparison. Everything it writes is UNVERIFIED.

Every write is recorded once (`effects.within`, in the step's own transaction) and guarded by the task's lease, so a
replayed node never calls the model or applies twice, and a stopped run writes nothing. State is ids, enums and
counts only: the model output lives on the task row (`resume_parse_tasks.staged`), never in a checkpoint.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.agents import effects
from app.services import resume_parsing as rp

GRAPH = "resume_parsing"
GRAPH_VERSION = "v1"
# The state keys safe to show in a trace (app.agents.tracing_boundary) and to store as the run's result.
EXPORT_FIELDS = ("resume_id", "task_id", "attempt", "step", "outcome", "roles", "bullets", "skills", "projects",
                 "education", "dropped", "baseline_roles", "baseline_bullets")


class ResumeParsingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume_id: int
    task_id: int
    attempt: int = 1


class ResumeParsingResume(BaseModel):
    """Never used: structuring waits for no person. Present because every graph names one."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["continue"]


class ResumeParsingState(TypedDict, total=False):
    resume_id: int
    task_id: int
    attempt: int
    step: str
    outcome: str
    roles: int
    bullets: int
    skills: int
    projects: int
    education: int
    dropped: int
    baseline_roles: int
    baseline_bullets: int


@dataclass(frozen=True)
class ResumeParsingContext:
    """Per-run context, never checkpointed: the thread lease, a session factory and the task's claim."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    hold: Callable[[str], None] | None = None
    task_id: int | None = None
    claim: Any = None  # resume_parsing.ParseClaim: its lock token guards every write, as on the plain path


def _hold(ctx: ResumeParsingContext, point: str) -> None:
    if ctx.hold is not None:
        ctx.hold(point)


def _within(ctx: ResumeParsingContext, state: ResumeParsingState) -> rp.Within:
    def record(db: Session, step: str) -> bool:
        return effects.within(db, ctx.run, f"resume_parse.{step}:{state['task_id']}:a{state.get('attempt', 1)}")[1]
    return record


def _generate(ctx: ResumeParsingContext) -> rp.Generate:
    """The model call through LangChain's interface: GatewayChatModel is LLMClient.generate_prompt, unchanged."""
    from app.agents.llm import GatewayChatModel, prompt_messages

    def generate(prompt: Any, call: Any) -> dict[str, Any]:
        model = GatewayChatModel(user_id=ctx.run.user_id, prompt=prompt, call=call, sessions=ctx.sessions)
        message = model.invoke(prompt_messages(prompt))
        return rp.checked(message.additional_kwargs.get("parsed") or {})
    return generate


def load(state: ResumeParsingState, runtime: Runtime[ResumeParsingContext]) -> dict[str, Any]:
    return rp.load(runtime.context.sessions, runtime.context.claim)


def extract(state: ResumeParsingState, runtime: Runtime[ResumeParsingContext]) -> dict[str, Any]:
    ctx = runtime.context
    out = rp.extract(ctx.sessions, ctx.claim, _generate(ctx), _within(ctx, state))
    _hold(ctx, "extract:after_effect")
    return out


def ground(state: ResumeParsingState, runtime: Runtime[ResumeParsingContext]) -> dict[str, Any]:
    ctx = runtime.context
    return rp.ground_step(ctx.sessions, ctx.claim, _within(ctx, state))


def check(state: ResumeParsingState, runtime: Runtime[ResumeParsingContext]) -> dict[str, Any]:
    ctx = runtime.context
    return rp.check(ctx.sessions, ctx.claim, _within(ctx, state))


def apply(state: ResumeParsingState, runtime: Runtime[ResumeParsingContext]) -> dict[str, Any]:
    ctx = runtime.context
    out = rp.apply(ctx.sessions, ctx.claim, _within(ctx, state))
    _hold(ctx, "apply:after_effect")
    return out


def _next(after: str) -> Callable[[ResumeParsingState], str]:
    def route(state: ResumeParsingState) -> str:
        return END if state.get("outcome") else after
    return route


def build(checkpointer: Any) -> Any:
    graph = StateGraph(ResumeParsingState, context_schema=ResumeParsingContext)
    graph.add_node("load", load)
    graph.add_node("extract", extract)
    graph.add_node("ground", ground)
    graph.add_node("check", check)
    graph.add_node("apply", apply)
    graph.add_edge(START, "load")
    graph.add_conditional_edges("load", _next("extract"), ["extract", END])
    graph.add_conditional_edges("extract", _next("ground"), ["ground", END])
    graph.add_conditional_edges("ground", _next("check"), ["check", END])
    graph.add_conditional_edges("check", _next("apply"), ["apply", END])
    graph.add_edge("apply", END)
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in EXPORT_FIELDS if k in state and state[k] is not None}
