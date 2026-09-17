"""LLM telemetry for the ADK agents, recorded through the same pipeline as LLMClient (app.services.llm_telemetry).

ADK calls Gemini itself, so these model callbacks time each call and read Gemini's usage_metadata. Calls made
through Vertex AI are recorded with provider "vertex_ai"; the model catalog prices only the Gemini API, so their
cost stays unknown rather than guessed.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from app.prompts.agents import AGENT_PROMPT_VERSION
from app.services import llm_telemetry
from app.services.llm_telemetry import Attempt, LLMCall, Usage

# Agent name → the prompt name whose stage the call belongs to (llm_telemetry.STAGE_BY_PROMPT).
AGENT_PROMPTS = {"job_fit_ranker": "ranking", "resume_tailor": "tailoring", "resume_reviewer": "review",
                 "follow_up_writer": "followup"}
_started: dict[tuple[str, str], tuple[float, Attempt]] = {}
_lock = threading.Lock()


def _provider() -> str:
    return "vertex_ai" if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes") else "gemini"


def _text(value: Any) -> str:
    """Plain text of ADK/genai contents, only to estimate context size; never stored."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(item) for item in value)
    parts = getattr(value, "parts", None)
    if parts is not None:
        return "\n".join(getattr(part, "text", None) or "" for part in parts)
    return str(value)


def _key(callback_context: Any) -> tuple[str, str]:
    return str(getattr(callback_context, "invocation_id", "")), str(getattr(callback_context, "agent_name", ""))


def before_model(callback_context: Any, llm_request: Any) -> None:
    agent = str(getattr(callback_context, "agent_name", ""))
    config = getattr(llm_request, "config", None)
    attempt = Attempt(provider=_provider(), requested_model=getattr(llm_request, "model", None), user_id=None,
                      call=LLMCall(workflow_type="agent"), prompt_name=AGENT_PROMPTS.get(agent),
                      prompt_version=AGENT_PROMPT_VERSION, system=_text(getattr(config, "system_instruction", None)),
                      user=_text(getattr(llm_request, "contents", None)), sent=True)
    with _lock:
        _started[_key(callback_context)] = (time.monotonic(), attempt)
    return None


def _finish(callback_context: Any, error: BaseException | None) -> Attempt | None:
    with _lock:
        started = _started.pop(_key(callback_context), None)
    return started


def after_model(callback_context: Any, llm_response: Any) -> None:
    started = _finish(callback_context, None)
    if started is None:
        return None
    began, attempt = started
    meta = getattr(llm_response, "usage_metadata", None)
    prompt = getattr(meta, "prompt_token_count", None)
    if isinstance(prompt, int):
        cached = getattr(meta, "cached_content_token_count", None)
        cached = cached if isinstance(cached, int) else None
        output = getattr(meta, "candidates_token_count", None)
        attempt.usage = Usage(input_tokens=prompt, output_tokens=output if isinstance(output, int) else None,
                              cache_read_tokens=cached, uncached_input_tokens=prompt - (cached or 0))
    version = getattr(llm_response, "model_version", None)
    attempt.model = version if isinstance(version, str) and version else attempt.requested_model
    attempt.response_text = _text(getattr(llm_response, "content", None))
    error_code = getattr(llm_response, "error_code", None)
    failure = RuntimeError(f"{error_code}: {getattr(llm_response, 'error_message', '')}") if error_code else None
    llm_telemetry.record(attempt, latency_ms=int((time.monotonic() - began) * 1000), error=failure,
                         error_type="PROVIDER_ERROR" if failure else None)
    return None


def on_model_error(callback_context: Any, llm_request: Any, error: Exception) -> None:
    started = _finish(callback_context, error)
    if started is None:
        return None
    began, attempt = started
    from app.services.llm import _provider_error, error_type

    typed = _provider_error("Gemini (ADK)", attempt.requested_model or "", error)
    llm_telemetry.record(attempt, latency_ms=int((time.monotonic() - began) * 1000), error=error,
                         error_type=error_type(typed))
    return None


CALLBACKS = {"before_model_callback": before_model, "after_model_callback": after_model,
             "on_model_error_callback": on_model_error}
