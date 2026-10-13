"""What a LangSmith trace may contain: an allowlist, then a fail-closed check of the bytes that would be sent.

Two independent layers, so a gap in one is caught by the other:

1. **Projection** (`project_*`, the LangSmith client's hide hooks and run transform): only allowlisted keys with
   short, plain values survive — graph, node, step, status, counts, the trace id, the worker, prompt name/version and
   the configured, requested and actual model. Graph thread ids become keyed pseudonyms; user ids, inputs, prompts,
   responses, documents and anything unknown are dropped.
2. **Wire check** (`wire_check`, run on every request body by app.services.external_api.requests_session): a body
   that cannot be read as text, that is compressed, that names a credential field or that contains anything
   secret-shaped is not sent at all.

No setting widens this: LLM_PROMPT_DEBUG, the capture switches and EXTERNAL_API_DEBUG_PAYLOADS do not apply here,
and there is no "export full payloads" option. Passwords, OTPs, cookies, OAuth/API tokens, authorization headers,
browser profiles and session storage never leave the process.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from app.agents import safety
from app.settings import get_settings

# Metadata keys LangGraph, LangChain and the runtime add that are safe to keep (after value checks).
METADATA_FIELDS = ("graph", "graph_version", "langgraph_node", "langgraph_step", "trace_id", "worker",
                   "prompt_name", "prompt_version")
# What the LLM adapter reports about a call (app.agents.llm, ChatResult.llm_output["jobhunt"]).
LLM_FIELDS = ("worker_type", "prompt_name", "prompt_version", "provider", "model", "configured", "requested_model",
              "model_group", "routing_tier", "mode", "fallback_used", "escalated", "escalation_reason",
              "config_version", "routing_cause", "latency_ms", "input_tokens", "output_tokens")
RUN_FIELDS = ("id", "trace_id", "dotted_order", "parent_run_id", "name", "run_type", "start_time", "end_time",
              "inputs", "outputs", "error", "extra", "tags", "events", "session_name", "session_id", "status")
# An update (Client.update_run) may only carry these; anything else (attachments, replicas) is dropped.
UPDATE_FIELDS = ("name", "run_type", "start_time", "end_time", "error", "inputs", "outputs", "events", "extra", "tags",
                 "trace_id", "parent_run_id", "dotted_order", "session_id", "session_name", "reference_example_id")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/\- ]{0,120}$")
_SAFE_TAG = re.compile(r"^[a-z0-9_:\-]{1,40}$")
_ERROR_CLASS = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]{0,80})")
_JSON_KEY = re.compile(rb'"([^"\\]{1,80})"\s*:')
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def pseudonym(value: str) -> str:
    """A stable, keyed stand-in for an identifier that names an account (a graph thread id)."""
    key = (get_settings().secret_key or "job-hunt-os-agent-traces").encode()
    return "t_" + hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:16]


def _plain(value: Any) -> Any:
    """The value if it is a short, plain scalar that is not secret-shaped; otherwise None (dropped)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and _SAFE_TEXT.match(value) and not safety.secret_shaped(value):
        return value
    return None


def _keep(source: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    kept = {}
    for name in fields:
        if name in source and not safety.denied_key(name):
            value = _plain(source[name])
            if value is not None:
                kept[name] = value
    return kept


def project_state(fields: tuple[str, ...]):
    """The hide_inputs / hide_outputs hook for a graph: its declared safe state fields, plus LLM attribution."""
    def hook(payload: dict) -> dict:
        kept = _keep(payload, fields)
        llm = (payload.get("llm_output") or {}).get("jobhunt") if isinstance(payload, dict) else None
        if isinstance(llm, dict):
            kept["llm"] = _keep(llm, LLM_FIELDS)
        return kept
    return hook


def project_metadata(metadata: dict) -> dict:
    kept = _keep(metadata, METADATA_FIELDS)
    thread_id = metadata.get("thread_id") if isinstance(metadata, dict) else None
    if isinstance(thread_id, str):
        kept["thread"] = pseudonym(thread_id)
    return kept


def project_error(error: Any) -> str | None:
    """Only the exception's class name: a message can quote a header, a URL with a password, or a token."""
    if error is None:
        return None
    match = _ERROR_CLASS.match(str(error))
    return match.group(1) if match else "Error"


def _envelope(clean: dict) -> dict:
    """The parts of a run around inputs and outputs: metadata only in `extra`, plain tags, a plain name, start and
    end events only (no token stream), an error class name."""
    if "extra" in clean:
        extra = clean["extra"]
        clean["extra"] = {"metadata": (extra or {}).get("metadata") or {}} if isinstance(extra, dict) else {}
    if clean.get("tags") is not None:
        clean["tags"] = [t for t in clean["tags"] if isinstance(t, str) and _SAFE_TAG.match(t)]
    if clean.get("name") is not None and _plain(clean["name"]) is None:
        clean["name"] = "run"
    if clean.get("events") is not None:
        clean["events"] = [{"name": e["name"], "time": e.get("time")} for e in clean["events"]
                           if isinstance(e, dict) and e.get("name") in ("start", "end")]
    if clean.get("error") is not None:
        clean["error"] = project_error(clean["error"])
    return clean


def project_run(run: dict) -> dict:
    """The final shape of a new run sent to LangSmith (after the hooks): allowlisted top-level fields only."""
    return _envelope({k: run[k] for k in RUN_FIELDS if k in run})


def project_update(fields: dict) -> dict:
    """The keyword arguments of Client.update_run, which does not pass through the run transform."""
    return _envelope({k: v for k, v in fields.items() if k in UPDATE_FIELDS})


def wire_check(body: bytes | None) -> str | None:
    """Why this request body must not be sent, or None. Fails closed on anything it cannot read."""
    if body is None or body == b"":
        return None
    if not isinstance(body, (bytes, bytearray)):
        return "body is not plain bytes"
    if body.startswith(_ZSTD_MAGIC):
        return "compressed body"
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        return "body is not text"
    if safety.secret_shaped(text):
        return "secret-shaped text"
    for match in _JSON_KEY.finditer(body):
        if safety.denied_key(match.group(1).decode("utf-8", "ignore")):
            return "credential field"
    return None
