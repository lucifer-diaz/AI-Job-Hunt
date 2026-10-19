"""Merge checkpoint (Phase 4): the current tailoring path and the preparation graph on the same cases.

Both paths tailor with the same (mock) Tailor and the same scripted Reviewer. On every case the graph saves the same
first version and reaches the same first review decision as the current path's review_results decision; neither ever
writes the source resume or ranks; and no version with an unsupported claim is ever passed. The graph then goes on where
the current path stops (its bounded revision loop), which the report shows per case.
"""
from __future__ import annotations

import pytest

from tests import preparation_support as ps
from tests import qualification_support as qs


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch):
    from app.agents import checkpoint

    ps.configure(monkeypatch, "on")
    checkpoint.forget_setup()
    qs.clear_checkpoints(pg_engine)
    return qs.app_sessions(pg_engine)


@pytest.mark.parametrize("case", ps.CASES, ids=[c[0] for c in ps.CASES])
def test_the_graph_tailors_and_decides_as_the_current_path(sessions, monkeypatch, case):
    out = ps.run_case(sessions, monkeypatch, case)
    assert out["same_tailoring"], out
    assert out["same_first_decision"], (out["current"]["first_decision"], out["graph"]["first_decision"])
    for path in ("current", "graph"):
        assert out[path]["source_resume_unchanged"] and out[path]["rankings"] == 0
    assert out["graph"]["passed_with_unsupported"] is False
