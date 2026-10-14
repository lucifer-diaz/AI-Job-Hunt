"""LangSmith export (app.agents.tracing_boundary): off, on and unavailable; no credential ever leaves the process.

Nothing here reaches LangSmith: `requests.Session.send`, the transport under the instrumented session, is replaced by
a recorder, so the assertions are on the exact bytes that would have been sent.
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

import pytest
import requests
from langgraph.graph import END, START, StateGraph

from app.agents import export_policy, runtime, tracing_boundary
from app.db import SessionLocal
from app.main import app  # noqa: F401  (migrates the test database)
from app.models import ExternalAPIRequestLog
from app.settings import get_settings
from tests import agents_support as support
from tests.conftest import make_user

S = support.SECRETS


class Wire:
    """Stands in for the network under the instrumented session."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.bodies, self.headers = mode, [], []

    def send(self, session: Any, request: Any, **kwargs: Any) -> requests.Response:
        body = request.body.encode() if isinstance(request.body, str) else (request.body or b"")
        self.bodies.append(bytes(body))
        self.headers.append(dict(request.headers))
        if self.mode == "down":
            raise requests.ConnectionError("connection refused")
        if self.mode == "timeout":
            raise requests.Timeout("read timed out")
        response = requests.Response()
        response.status_code = 503 if self.mode == "503" else 200
        response._content, response.request, response.url = b"{}", request, request.url
        return response

    def text(self) -> str:
        return b"\n".join(self.bodies).decode("utf-8", "replace")


@pytest.fixture
def wire(monkeypatch):
    recorder = Wire()
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kw: recorder.send(self, request, **kw))
    tracing_boundary.reset()
    yield recorder
    tracing_boundary.flush()
    tracing_boundary.reset()


@pytest.fixture
def export_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_graphs_enabled", True)
    monkeypatch.setattr(settings, "agent_langsmith_export_enabled", True)
    monkeypatch.setattr(settings, "agent_langsmith_api_key", "lsv2_pt_SYNTHETIC_langsmith_key")
    # Every debug and capture switch on: none of them may widen what is exported.
    monkeypatch.setattr(settings, "llm_prompt_debug", True)
    monkeypatch.setattr(settings, "external_api_debug_payloads", True)


def run_pilot(user_id: int, kind: str = "review") -> int:
    saver = support.memory_saver()
    pk = support.start(SessionLocal, user_id, {"kind": kind, "items": 3})
    runtime.execute(SessionLocal, support.claim(SessionLocal, user_id, pk), saver=saver)
    if kind == "review":
        run, command = support.resume(SessionLocal, user_id, pk, "approve")
        runtime.execute(SessionLocal, run, command, saver=saver)
    tracing_boundary.flush()
    return pk


def export_rows(since_id: int) -> list[ExternalAPIRequestLog]:
    with SessionLocal() as db:
        return list(db.query(ExternalAPIRequestLog).filter(ExternalAPIRequestLog.provider == "LANGSMITH",
                                                           ExternalAPIRequestLog.id > since_id))


def last_log_id() -> int:
    with SessionLocal() as db:
        return db.query(ExternalAPIRequestLog.id).order_by(ExternalAPIRequestLog.id.desc()).limit(1).scalar() or 0


def assert_no_secret(text: str) -> None:
    for name, value in S.items():
        if name == "otp":
            assert '"otp"' not in text and "482915" not in text
        else:
            assert value not in text and value.split("=")[-1] not in text, name
    for field in ('"authorization"', '"cookie"', '"password"', '"api_key"', '"storage_state"', '"access_token"'):
        assert field not in text.lower(), field


# ───────────── off ─────────────

def test_disabled_export_sends_nothing_even_when_the_environment_asks_for_tracing(wire, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_SYNTHETIC_env_key")
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", True)
    made = []
    import langsmith

    original = langsmith.Client.__init__
    monkeypatch.setattr(langsmith.Client, "__init__", lambda self, *a, **k: made.append(1) or original(self, *a, **k))
    pk = run_pilot(make_user(SessionLocal))
    assert support.thread(SessionLocal, pk).status == "COMPLETED"
    assert wire.bodies == [] and made == []
    assert tracing_boundary.configured() is False


def test_export_needs_both_the_switch_and_a_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_langsmith_export_enabled", True)
    assert tracing_boundary.configured() is False  # no key
    monkeypatch.setattr(settings, "agent_langsmith_export_enabled", False)
    monkeypatch.setattr(settings, "agent_langsmith_api_key", "lsv2_pt_SYNTHETIC")
    assert tracing_boundary.configured() is False  # no switch


# ───────────── on ─────────────

def test_enabled_export_sends_safe_metadata_only(wire, export_on):
    before = last_log_id()
    pk = run_pilot(make_user(SessionLocal))
    assert support.thread(SessionLocal, pk).status == "COMPLETED"
    text = wire.text()
    assert wire.bodies and "/runs" in str(export_rows(before)[0].endpoint_path)
    assert '"langgraph_node":"approval"' in text and '"graph":"pilot"' in text  # the useful part arrives
    thread_id = support.thread(SessionLocal, pk).thread_id
    assert thread_id not in text and export_policy.pseudonym(thread_id) in text  # the owner is not named
    assert '"user_id"' not in text and '"runtime"' not in text
    assert all(not body.startswith(b"\x28\xb5\x2f\xfd") for body in wire.bodies)  # never compressed
    rows = export_rows(before)
    assert rows and all(r.status == "SUCCEEDED" for r in rows)
    assert all(r.request_metadata is None or "payload" not in json.dumps(r.request_metadata) for r in rows)


class LeakyState(TypedDict, total=False):
    kind: str
    items: int
    note: str
    password: str
    authorization: str
    cookie: str
    otp: str
    storage_state: dict


def leaky_graph(fail: bool):
    """A graph whose state, metadata and errors are full of credentials: what a buggy future graph might do."""
    def node(state: LeakyState) -> dict:
        if fail:
            raise RuntimeError(f"upstream said 401 for {S['bearer']} cookie={S['cookie']} key={S['openai']}")
        return {"note": f"token {S['google_access']} and {S['anthropic']}", "items": 2}
    graph = StateGraph(LeakyState)
    graph.add_node("leak", node)
    graph.add_edge(START, "leak")
    graph.add_edge("leak", END)
    return graph.compile()


@pytest.mark.parametrize("fail", [False, True], ids=["success", "error"])
def test_no_credential_reaches_langsmith_even_from_a_leaky_graph(wire, export_on, fail):
    state = {"kind": "auto", "items": 1, "note": f"refresh {S['google_refresh']} jwt {S['jwt']}",
             "password": S["password"], "authorization": S["bearer"], "cookie": S["cookie"], "otp": S["otp"],
             "storage_state": {"cookies": [{"name": "sid", "value": S["cookie"]}], "origins": []}}
    config = {"configurable": {"thread_id": "pilot:u1:" + "a" * 32 + ":v1", "api_key": S["openai"],
                               "client_secret": S["google_client_secret"]},
              "metadata": {"authorization": S["bearer"], "graph": "leaky", "note": S["anthropic"],
                           "trace_id": f"trace {S['jwt']}"},
              "tags": [f"key:{S['openai']}", "safe-tag"], "run_name": f"run {S['openai']}"}
    with tracing_boundary.export(("kind", "items", "note")) as callbacks:
        config["callbacks"] = callbacks
        try:
            leaky_graph(fail).invoke(state, config=config)
        except RuntimeError:
            assert fail
    tracing_boundary.flush()
    assert wire.bodies, "the run should still be exported (safely)"
    text = wire.text()
    assert_no_secret(text)
    assert '"safe-tag"' in text and '"graph":"leaky"' in text
    if fail:
        assert '"RuntimeError"' in text and "401" not in text  # the class name only


def test_the_wire_check_refuses_what_the_projection_missed():
    assert export_policy.wire_check(b'{"inputs": {"kind": "auto"}}') is None
    assert export_policy.wire_check(None) is None
    for body in (json.dumps({"x": S["openai"]}).encode(), b'{"password": "x"}', b'{"Authorization": "x"}',
                 b'{"storage_state": {}}', b"\x28\xb5\x2f\xfdcompressed", b"\xff\xfe\x00binary"):
        assert export_policy.wire_check(body) is not None, body


def test_a_refused_body_is_never_sent_and_is_recorded(wire, monkeypatch):
    from app.services.external_api import requests_session

    before = last_log_id()
    session = requests_session("LANGSMITH", service="runs", operation="trace_export",
                               pre_send=export_policy.wire_check)
    response = session.post("https://api.smith.langchain.com/runs", data=json.dumps({"note": S["anthropic"]}))
    assert response.status_code == 400 and wire.bodies == []
    rows = export_rows(before)
    assert len(rows) == 1 and rows[0].error_type == "CLIENT_ERROR" and rows[0].http_status_code is None
    assert rows[0].request_metadata == {"blocked_by": "pre_send_policy"}
    assert S["anthropic"] not in (rows[0].error_message or "")


def test_a_body_the_check_cannot_read_is_never_sent(wire):
    """A streamed body (LangSmith streams multipart batches over its inline limit) fails closed."""
    from app.services.external_api import requests_session

    before = last_log_id()
    session = requests_session("LANGSMITH", service="runs", operation="trace_export",
                               pre_send=export_policy.wire_check)
    response = session.post("https://api.smith.langchain.com/runs/multipart",
                            data=iter([json.dumps({"note": S["anthropic"]}).encode()]))
    assert response.status_code == 400 and wire.bodies == []
    rows = export_rows(before)
    assert len(rows) == 1 and rows[0].request_metadata == {"blocked_by": "pre_send_policy"}


# ───────────── unavailable ─────────────

@pytest.mark.parametrize("mode", ["down", "timeout", "503"])
def test_a_langsmith_outage_changes_nothing(wire, export_on, mode):
    wire.mode = mode
    before = last_log_id()
    user = make_user(SessionLocal)
    pk = run_pilot(user)
    row = support.thread(SessionLocal, pk)
    assert row.status == "COMPLETED" and row.result["outcome"] == "completed"
    assert len(support.effects(SessionLocal, pk)) == 1
    rows = export_rows(before)
    assert rows and all(r.status == "FAILED" for r in rows)
    assert {r.error_type for r in rows} <= {"CONNECTION", "TIMEOUT", "SERVER_ERROR", "OTHER", "SDK_ERROR"}
