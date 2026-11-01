"""The pilot graph: shows branching, a checkpointed interrupt and restart recovery, with no production side effect.

    validate → compute ─┬─ kind=auto ─────────────────────────────→ finalize → END
                        └─ kind=review → approval (interrupt) ─┬─ approve → finalize → END
                                                               └─ reject  → rejected → END

It is deterministic (no model, no network). Its only write is one synthetic `agent_effects` row ("pilot.completed")
made through `effects.once`, which is how the tests show that a replayed node never repeats an effect.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents import effects

GRAPH = "pilot"
GRAPH_VERSION = "v1"
MAX_ITEMS = 20
# The state keys safe to show in a trace (app.agents.tracing_boundary): ids, enums and counts.
EXPORT_FIELDS = ("kind", "items", "total", "decision", "outcome", "step")


class PilotInput(BaseModel):
    """What a person may start the pilot with. Nothing else reaches the graph."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["auto", "review"] = "auto"
    items: int = Field(default=3, ge=1, le=MAX_ITEMS)


class PilotDecision(BaseModel):
    """The only value a resume carries."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]


class PilotState(TypedDict, total=False):
    kind: str
    items: int
    total: int
    decision: str
    outcome: str
    step: int


@dataclass(frozen=True)
class AgentContext:
    """Per-run context. LangGraph never checkpoints it: the lease and the database stay out of graph state."""

    run: effects.RunHandle
    sessions: Callable[[], Session]
    # A hook called at named points (tests use it to stop a process mid-run); None in production.
    hold: Callable[[str], None] | None = None


def _hold(runtime: Runtime[AgentContext], point: str) -> None:
    if runtime.context is not None and runtime.context.hold is not None:
        runtime.context.hold(point)


def validate(state: PilotState) -> dict[str, Any]:
    """Check the input against PilotInput; nothing else reaches the graph."""
    clean = PilotInput(kind=state.get("kind", "auto"), items=state.get("items", 3))
    return {"kind": clean.kind, "items": clean.items, "step": 1}


def compute(state: PilotState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Add up the items: pure computation, no side effect."""
    _hold(runtime, "compute")
    return {"total": sum(range(1, state["items"] + 1)), "step": 2}


def approval(state: PilotState) -> dict[str, Any]:
    """Pause for a person (interrupt) when the run asked for review; their decision resumes it."""
    answer = interrupt({"kind": "approval", "items": state["items"], "total": state["total"]})
    decision = PilotDecision.model_validate(answer if isinstance(answer, dict) else {"decision": answer})
    return {"decision": decision.decision, "step": 3}


def finalize(state: PilotState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Record the one side effect of the pilot, exactly once per thread (effects.once)."""
    ctx = runtime.context
    with ctx.sessions() as db:
        effects.once(db, ctx.run, "pilot.completed", {"items": state["items"], "total": state["total"]})
    _hold(runtime, "finalize:after_effect")
    return {"outcome": "completed", "step": 4}


def rejected(state: PilotState) -> dict[str, Any]:
    """End without any side effect: the person declined."""
    return {"outcome": "rejected", "step": 4}


def _after_compute(state: PilotState) -> str:
    return "approval" if state["kind"] == "review" else "finalize"


def _after_approval(state: PilotState) -> str:
    return "finalize" if state.get("decision") == "approve" else "rejected"


def build(checkpointer: Any) -> Any:
    graph = StateGraph(PilotState, context_schema=AgentContext)
    graph.add_node("validate", validate)
    graph.add_node("compute", compute)
    graph.add_node("approval", approval)
    graph.add_node("finalize", finalize)
    graph.add_node("rejected", rejected)
    graph.add_edge(START, "validate")
    graph.add_edge("validate", "compute")
    graph.add_conditional_edges("compute", _after_compute, ["approval", "finalize"])
    graph.add_conditional_edges("approval", _after_approval, ["finalize", "rejected"])
    graph.add_edge("finalize", END)
    graph.add_edge("rejected", END)
    return graph.compile(checkpointer=checkpointer, name=GRAPH)


def summary(state: dict[str, Any]) -> dict[str, Any]:
    """The finished run's result as stored on the thread: safe fields only."""
    return {k: state[k] for k in EXPORT_FIELDS if k in state}
