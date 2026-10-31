"""AI Orchestration's LangGraph view (app.services.graph_topology): every registered graph drawn from its compiled
structure and its nodes' own docstrings, never by hand. No model, checkpointer or graph run is used."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.agents import runtime
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import AgentThread
from app.services import graph_topology
from app.services import orchestration_topology as topo
from tests.conftest import make_user


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", False))
        return TestClient(app)
    return sign_in


def test_every_registered_graph_is_drawn_from_its_compiled_structure():
    drawn = {g["name"]: g for g in graph_topology.structure()}
    assert set(drawn) == set(runtime.GRAPHS)
    for name, spec in runtime.GRAPHS.items():
        compiled = spec.build(None).get_graph()
        g = drawn[name]
        assert [n["id"] for n in g["nodes"]] == list(compiled.nodes)
        real = {(e.source, e.target) for e in compiled.edges}
        assert {(e["source"], e["target"]) for e in g["edges"]} <= real  # early exits are shown on the node instead
        for n in g["nodes"]:
            if n["kind"] == "step":
                assert n["doc"], f"{name}.{n['id']} has no docstring to show"
        # A loop goes back to an earlier step; everything else moves forward one column or more.
        cols = {n["id"]: n["depth"] for n in g["nodes"]}
        assert all(e["loop"] or cols[e["target"]] > cols[e["source"]] for e in g["edges"])
        if spec.orchestration_node:
            assert topo.node(spec.orchestration_node) is not None and g["orchestration_label"]


def test_pauses_loops_and_early_stops_are_the_codes_own():
    drawn = {g["name"]: g for g in graph_topology.structure()}
    nodes = lambda graph: {n["id"]: n for n in drawn[graph]["nodes"]}
    assert nodes("qualification")["wait"]["interrupt"] and not nodes("qualification")["rank"]["interrupt"]
    assert nodes("pilot")["approval"]["interrupt"]
    assert nodes("qualification")["admission"]["may_stop"]  # a hidden job ends at admission
    prep = {(e["source"], e["target"]): e for e in drawn["preparation"]["edges"]}
    assert prep[("decide", "tailor")]["loop"] and prep[("decide", "tailor")]["conditional"]  # the revision loop
    assert not prep[("__start__", "select_resume")]["conditional"]


def test_the_view_counts_only_the_accounts_own_runs_and_reports_modes(signed_in, monkeypatch):
    from app.settings import get_settings

    owner, other = make_user(SessionLocal), make_user(SessionLocal)
    with SessionLocal() as db:
        for uid, status in ((owner, "COMPLETED"), (owner, "INTERRUPTED"), (other, "FAILED")):
            db.add(AgentThread(user_id=uid, thread_id=f"pilot:u{uid}:{uuid.uuid4().hex}:v1", graph="pilot",
                               graph_version="v1", subject=uuid.uuid4().hex, status=status))
        db.commit()
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", False)
    out = signed_in(owner).get("/api/orchestration/langgraph").json()
    pilot = next(g for g in out["graphs"] if g["name"] == "pilot")
    assert (pilot["runs"]["COMPLETED"], pilot["runs"]["INTERRUPTED"], pilot["runs"]["FAILED"]) == (1, 1, 0)
    assert pilot["mode"]["effective"] == "off" and pilot["mode"]["note"]
    assert {s["id"] for s in out["runtime"]} >= {"lease", "graph", "interrupt", "state"}
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", True)
    monkeypatch.setattr(get_settings(), "qualification_graph_mode", "shadow")
    graphs = {g["name"]: g for g in signed_in(other).get("/api/orchestration/langgraph").json()["graphs"]}
    assert graphs["qualification"]["mode"]["effective"] == "shadow" and graphs["pilot"]["runs"]["FAILED"] == 1
    # Every graph reads its own switch: resume parsing runs by default (RESUME_PARSE_GRAPH_MODE=on).
    assert graphs["resume_parsing"]["mode"]["setting"] == "RESUME_PARSE_GRAPH_MODE"
    assert graphs["resume_parsing"]["mode"]["effective"] == "on"
    monkeypatch.setattr(get_settings(), "resume_parse_graph_mode", "off")
    graphs = {g["name"]: g for g in signed_in(other).get("/api/orchestration/langgraph").json()["graphs"]}
    assert graphs["resume_parsing"]["mode"]["running"] is False


def test_every_registered_graph_names_its_mode_setting():
    from app.agents import runtime
    from app.services import graph_topology

    assert set(runtime.GRAPHS) - {"pilot"} <= set(graph_topology.MODE_SETTINGS)
