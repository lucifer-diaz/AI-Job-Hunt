"""The LangGraph architecture view of AI Orchestration: every registered graph's real structure, read from the code.

    structure   app.agents.runtime.GRAPHS → each graph's compiled StateGraph (`get_graph()`): nodes and edges exactly as
                LangGraph runs them, whether each edge is conditional, where a run can stop early
    describe    each node function's own docstring (its first paragraph), and whether it calls `interrupt(` (a pause
                for a worker or a person, resumed from the PostgreSQL checkpoint)
    state       the mode each graph runs in (settings), and the account's runs per status (agent_threads)

Read-only like the rest of orchestration: nothing is drawn by hand, no stage name is hard-coded in the frontend, no
model is called. A run's current node lives only in its checkpoint, which this view does not open, so it reports run
counts per status and never guesses where a run is.
"""
from __future__ import annotations

import inspect
import re
import threading
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AGENT_THREAD_STATUSES, AgentThread
from app.services import orchestration_topology as topo
from app.settings import get_settings

START, END = "__start__", "__end__"
DOC_LIMIT = 320
_lock = threading.Lock()
_structure: list[dict[str, Any]] | None = None

# How each graph is switched on (README §9c). The pilot runs whenever agent graphs are on.
MODE_SETTINGS = {"qualification": "qualification_graph_mode", "preparation": "preparation_graph_mode",
                 "job_insights": "job_insights_graph_mode", "application": "application_execution_mode"}
LABELS = {"pilot": "Pilot (no side effects)", "qualification": "Qualification graph",
          "preparation": "Preparation graph", "application": "Application graph", "job_insights": "Job Insights graph"}


def _function(node: Any) -> Any:
    runnable = getattr(node, "runnable", None)
    return getattr(runnable, "func", None) or getattr(runnable, "afunc", None)


def _doc(fn: Any) -> str:
    """The node's own description: its docstring's first paragraph, on one line, capped."""
    text = (inspect.getdoc(fn) or "").split("\n\n")[0]
    text = re.sub(r"\s+", " ", text.replace("`", "")).strip()
    return text if len(text) <= DOC_LIMIT else text[:DOC_LIMIT - 1].rstrip() + "…"


def _pauses(fn: Any) -> bool:
    try:
        return "interrupt(" in inspect.getsource(fn)
    except (OSError, TypeError):
        return False


def _layout(nodes: list[str], edges: list[dict[str, Any]]) -> dict[str, int]:
    """A column per node: the longest path from START once loops are set aside. A graph declares its steps in the
    order they run (`add_node`, the shared `wait` last), so an edge back to an earlier step is a loop and is drawn as
    one. Deterministic."""
    order = {n: i for i, n in enumerate(nodes)}
    order[START], order[END] = -1, len(nodes)
    for e in edges:
        e["loop"] = order[e["target"]] <= order[e["source"]]
    depth = dict.fromkeys(nodes, 0)
    for _ in nodes:  # relax: longest path over the acyclic part
        for e in edges:
            if not e["loop"]:
                depth[e["target"]] = max(depth[e["target"]], depth[e["source"]] + 1)
    return depth


def _graph(name: str, spec: Any) -> dict[str, Any]:
    compiled = spec.build(None)
    drawn = compiled.get_graph()
    functions = {n: _function(node) for n, node in compiled.builder.nodes.items()}
    edges = [{"source": e.source, "target": e.target, "conditional": bool(e.conditional)} for e in drawn.edges]
    names = list(drawn.nodes)
    depth = _layout(names, edges)
    # A conditional exit to END from a step that has somewhere else to go is a stop ("nothing to do", a lost lease,
    # a refusal), shown on the node; the graph's normal finish stays an edge.
    early = {e["source"] for e in edges if e["target"] == END and e["conditional"]
             and any(o["source"] == e["source"] and o["target"] != END for o in edges)}
    nodes = []
    for n in names:
        fn = functions.get(n)
        kind = "start" if n == START else "end" if n == END else "step"
        nodes.append({"id": n, "kind": kind, "depth": depth[n], "doc": _doc(fn) if fn else "",
                      "interrupt": bool(fn) and _pauses(fn), "may_stop": n in early})
    shown = [e for e in edges if not (e["target"] == END and e["source"] in early)]
    node = topo.node(spec.orchestration_node) if spec.orchestration_node else None
    return {"name": name, "label": LABELS.get(name, name.replace("_", " ").title()), "version": spec.version,
            "worker_owned": not spec.api_resumable, "orchestration_node": spec.orchestration_node,
            "orchestration_label": node.label if node else None, "export_fields": list(spec.export_fields),
            "nodes": nodes, "edges": shown, "columns": max(depth.values()) + 1}


def structure() -> list[dict[str, Any]]:
    """Every registered graph, compiled once per process (the code does not change while it runs)."""
    global _structure
    with _lock:
        if _structure is None:
            from app.agents import runtime

            _structure = [_graph(name, spec) for name, spec in runtime.GRAPHS.items()]
        return _structure


def mode(name: str) -> dict[str, Any]:
    settings = get_settings()
    enabled = bool(settings.agent_graphs_enabled)
    configured = "on" if name == "pilot" else str(getattr(settings, MODE_SETTINGS.get(name, ""), "off") or "off")
    effective = configured if enabled else "off"
    return {"configured": configured, "effective": effective, "running": effective not in ("off", ""),
            "setting": MODE_SETTINGS.get(name, "agent_graphs_enabled").upper(),
            "note": None if enabled else "AGENT_GRAPHS_ENABLED is off: no graph runs"}


def runs(db: Session, user_id: int) -> dict[str, dict[str, int]]:
    """The account's own runs per graph and status. One grouped query."""
    rows = db.execute(select(AgentThread.graph, AgentThread.status, func.count(AgentThread.id))
                      .where(AgentThread.user_id == user_id).group_by(AgentThread.graph, AgentThread.status)).all()
    out: dict[str, Counter] = defaultdict(Counter)
    for graph, status, n in rows:
        out[graph][status] += n
    return {g: {s: c.get(s, 0) for s in AGENT_THREAD_STATUSES} for g, c in out.items()}


RUNTIME = [
    {"id": "scheduler", "label": "Scheduler / API", "detail": "starts a run or claims one whose lease expired"},
    {"id": "lease", "label": "Lease", "detail": "agent_threads.lock_token: one runner per thread"},
    {"id": "graph", "label": "LangGraph run", "detail": "nodes in order; each checkpoint saved in PostgreSQL"},
    {"id": "services", "label": "Domain services", "detail": "a node calls the service that owns its step, "
                                                             "once per key (effects.once)"},
    {"id": "workers", "label": "Workers", "detail": "recovery, parsing, ranking, tailoring, the browser: own task rows"},
    {"id": "interrupt", "label": "Interrupt", "detail": "waits for a worker or a person; resumed from the checkpoint"},
    {"id": "state", "label": "Business state", "detail": "only work_queue.transition, workflow and apply_queue move it"},
]


def view(db: Session, user_id: int) -> dict[str, Any]:
    counts = runs(db, user_id)
    graphs = [{**g, "mode": mode(g["name"]), "runs": counts.get(g["name"], dict.fromkeys(AGENT_THREAD_STATUSES, 0))}
              for g in structure()]
    return {"graphs": graphs, "runtime": RUNTIME,
            "note": "Drawn from the compiled graphs and their nodes' own docstrings. A run's current step lives only "
                    "in its checkpoint, so runs are counted per status, not placed on a step."}
