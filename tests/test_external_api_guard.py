"""Production code must not open its own connection to a third party.

Telemetry is a property of the HTTP client (app.services.external_api), so an integration that builds its own
client is invisible to Monitoring no matter how carefully it is written. This test is the guard: it fails when a
new unmanaged client appears in app/, and names the file so the fix is obvious.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
# The infrastructure that is allowed to construct real clients, and why.
APPROVED = {
    "app/services/external_api.py": "the instrumentation layer itself",
}
# Constructors that open an unmanaged connection to a third party.
BANNED_CALLS = {
    ("httpx", "Client"), ("httpx", "AsyncClient"), ("httpx", "stream"), ("httpx", "get"), ("httpx", "post"),
    ("httpx", "put"), ("httpx", "delete"), ("httpx", "patch"), ("httpx", "request"),
    ("requests", "get"), ("requests", "post"), ("requests", "put"), ("requests", "delete"), ("requests", "patch"),
    ("requests", "request"), ("requests", "Session"),
    ("aiohttp", "ClientSession"), ("urllib", "urlopen"),
}
BANNED_IMPORTS = {"requests", "aiohttp", "urllib3"}


def _module_attr(node: ast.AST) -> tuple[str, str] | None:
    """`httpx.Client` → ("httpx", "Client"); anything else → None."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None


def unmanaged_clients(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and (pair := _module_attr(node.func)) in BANNED_CALLS:
            found.append(f"{pair[0]}.{pair[1]}()")
        elif isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] in BANNED_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in BANNED_IMPORTS:
            found.append(f"from {node.module} import …")
    return found


def test_production_code_uses_only_the_instrumented_client():
    offenders = {}
    for path in sorted(APP.rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        if relative in APPROVED or "migrations" in path.parts:
            continue
        if hits := unmanaged_clients(path.read_text()):
            offenders[relative] = hits
    assert offenders == {}, (
        "These modules talk to a third party without telemetry. Use app.services.external_api.external_client "
        f"(or external_operation for an SDK) instead: {offenders}")


def test_the_guard_catches_a_new_unmanaged_client():
    """The detector has to actually detect; otherwise the test above passes for the wrong reason."""
    assert unmanaged_clients("import httpx\nwith httpx.Client() as c: c.get('https://x.test')") == ["httpx.Client()"]
    assert unmanaged_clients("import requests\nrequests.get('https://x.test')") == ["import requests", "requests.get()"]
    assert unmanaged_clients("import aiohttp\naiohttp.ClientSession()") == ["import aiohttp", "aiohttp.ClientSession()"]
    # What good code looks like: the shared client, and httpx used only for its types and errors.
    assert unmanaged_clients("from app.services.external_api import external_client\n"
                             "with external_client('APIFY') as c: c.get('https://api.apify.com')") == []
    assert unmanaged_clients("import httpx\ntry: ...\nexcept httpx.HTTPError: ...") == []


def test_only_the_instrumentation_layer_is_approved():
    """The allowlist stays tiny on purpose: every entry is a place telemetry could be bypassed."""
    assert set(APPROVED) == {"app/services/external_api.py"}
    for relative in APPROVED:
        assert (ROOT / relative).exists(), f"{relative} is allowlisted but does not exist"


@pytest.mark.parametrize("module", ["app/services/sources.py", "app/services/llm.py", "app/workspace.py"])
def test_every_migrated_integration_goes_through_the_layer(module):
    """The integrations that existed before this layer did now use it."""
    source = (ROOT / module).read_text()
    assert "app.services.external_api" in source or "external_client" in source or "apify_client" in source


# ───────────── trace export (LangSmith) ─────────────

# The one module that may build a LangSmith client or tracer: its client sends through the instrumented session and
# applies the export policy (app.agents.export_policy). Anywhere else, a tracer would export unpoliced payloads.
TRACE_EXPORT_BOUNDARY = "app/agents/tracing_boundary.py"
TRACE_EXPORTERS = {"Client", "LangChainTracer", "RunTree", "traceable", "tracing_context"}


def trace_exporters(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("langsmith", "langchain_core"):
            names = {a.name for a in node.names}
            if (node.module or "").startswith("langsmith") or "LangChainTracer" in names:
                found += [f"from {node.module} import {n}" for n in sorted(names & TRACE_EXPORTERS)]
        elif isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] == "langsmith"]
    return found


def test_only_the_tracing_boundary_builds_a_langsmith_exporter():
    offenders = {}
    for path in sorted(APP.rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        if relative == TRACE_EXPORT_BOUNDARY:
            continue
        if hits := trace_exporters(path.read_text()):
            offenders[relative] = hits
    assert offenders == {}, f"LangSmith exporters belong in {TRACE_EXPORT_BOUNDARY} only: {offenders}"
    assert trace_exporters("from langsmith import Client") == ["from langsmith import Client"]
    assert trace_exporters("from langchain_core.tracers import LangChainTracer") == [
        "from langchain_core.tracers import LangChainTracer"]
    assert trace_exporters("import langsmith") == ["import langsmith"]
    assert trace_exporters("from langchain_core.messages import AIMessage") == []


# Phase 7: the other Python trees that run beside the app. A script, the ADK agent or a sidecar that built its own
# tracer would export outside the policy as surely as one under app/.
OTHER_TREES = ("job_hunt_agent", "scripts", "sidecars")


def test_no_langsmith_exporter_outside_the_app_either():
    offenders = {}
    for tree in OTHER_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if hits := trace_exporters(path.read_text()):
                offenders[str(path.relative_to(ROOT))] = hits
    assert offenders == {}, f"LangSmith exporters belong in {TRACE_EXPORT_BOUNDARY} only: {offenders}"


def test_the_browser_use_runner_loads_no_tracing_and_turns_telemetry_off():
    """The runner drives a browser with sessions and logins: no LangChain/LangSmith import, no callback, and
    browser-use's own telemetry off before it is imported."""
    runner = ROOT / "sidecars" / "browser_use_runner" / "runner.py"
    tree = ast.parse(runner.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"langsmith", "langchain", "langchain_core", "langchain_openai", "langchain_anthropic"}
    source = runner.read_text()
    assert "ANONYMIZED_TELEMETRY" in source and "Agent(" not in source
