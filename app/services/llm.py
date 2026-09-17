"""Provider-independent LLM layer: one call shape for OpenRouter, OpenAI, Anthropic, Gemini and the no-cost mock.

Every request goes through `LLMClient._generate`, which records telemetry (app.services.llm_telemetry): tokens,
context size, latency, estimated cost, errors and structured-output validity. Services never add monitoring code;
they may pass an `LLMCall` saying which job or application the request is for, the attempt number, and a validator.

Failures are typed so workers can decide what to do without reading error text:

* LLMConfigError: missing key, unsupported provider, or a paid model while OPENROUTER_REQUIRE_FREE is on.
  Raised before any network call; retrying cannot help until the settings change.
* LLMTimeoutError / LLMRateLimitError: the provider was slow or throttled; worth retrying later.
* LLMContextOverflowError: the prompt does not fit the model's context window.
* LLMResponseError: the model answered, but not with a JSON object.
* LLMError: any other provider failure (outage, 5xx, connection error).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services import llm_telemetry
from app.services.ai_config import is_free_model, resolve as resolve_ai_config
from app.services.llm_telemetry import Attempt, LLMCall, Usage
from app.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.prompts.common import Prompt

__all__ = ["LLMCall", "LLMClient", "LLMConfigError", "LLMContextOverflowError", "LLMError", "LLMRateLimitError",
           "LLMResponseError", "LLMResult", "LLMTimeoutError"]


class LLMError(RuntimeError):
    pass


class LLMConfigError(LLMError):
    """The configured provider cannot be called as set up."""


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMContextOverflowError(LLMError):
    """The request is larger than the model's context window."""


class LLMResponseError(LLMError):
    """The provider answered, but the answer is not a usable JSON object."""


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str  # the model that answered when the provider reports it, otherwise the one requested
    usage: Usage | None = None
    validated: Any = None  # what LLMCall.validate returned, when a validator was given


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise LLMResponseError("Model did not return JSON")
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError("Model returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise LLMResponseError(f"Model returned a JSON {type(value).__name__}, not an object")
    return value


# Prompt markers (app.prompts) → the stage whose model runs them.
MARKER_TASKS = {"FIT_ANALYSIS": "rank", "TAILOR_RESUME": "tailor", "REVIEW_RESUME": "review", "FOLLOW_UP": "followup",
                "EXTRACT_EVIDENCE": "tailor", "AI_OPTIMIZATION": "advisor"}
CONTEXT_OVERFLOW = re.compile(r"context[_ ]length|context window|maximum context|prompt is too long|too many tokens", re.I)


def _task_from_prompt(user: str) -> str:
    """The stage of a prompt, read from its first line only, so text inside the data can never reroute it."""
    first_line = (user or "").lstrip().split("\n", 1)[0].strip()
    return MARKER_TASKS.get(first_line, "default")


def _csv_models(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_free_openrouter_model(model: str) -> bool:
    # The free router itself and explicit :free variants have zero token price.
    return is_free_model("openrouter", model)


def _provider_error(provider: str, model: str, exc: Exception) -> LLMError:
    """Map an SDK or transport exception to a typed LLMError without importing every SDK."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    detail = f"{provider} request failed for model={model}: {exc}"
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return LLMTimeoutError(detail)
    if status == 429 or "RateLimit" in name:
        return LLMRateLimitError(detail)
    if status == 413 or CONTEXT_OVERFLOW.search(str(exc)):
        return LLMContextOverflowError(detail)
    return LLMError(detail)


def error_type(exc: BaseException, *, validation: bool = False) -> str:
    """The Monitoring tab's failure category for an exception raised while generating."""
    if validation:
        return "SCHEMA_VALIDATION"
    return ("CONFIGURATION" if isinstance(exc, LLMConfigError) else "TIMEOUT" if isinstance(exc, (LLMTimeoutError, TimeoutError))
            else "RATE_LIMIT" if isinstance(exc, LLMRateLimitError)
            else "CONTEXT_OVERFLOW" if isinstance(exc, LLMContextOverflowError)
            else "INVALID_JSON" if isinstance(exc, LLMResponseError)
            else "PROVIDER_ERROR" if isinstance(exc, LLMError) else "OTHER")


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _answered_model(response: Any, requested: str) -> str:
    answered = getattr(response, "model", None)
    return answered if isinstance(answered, str) and answered else requested


class LLMClient:
    """Calls the AI provider chosen in a user's settings, with that user's own API key."""

    def __init__(self, overrides: dict[str, Any] | None = None, keys: dict[str, str | None] | None = None,
                 user_id: int | None = None):
        self.settings = get_settings()
        self.overrides = overrides or {}
        self.keys = keys or {}
        self.user_id = user_id

    @classmethod
    def for_user(cls, db: Session, user_id: int | None = None) -> LLMClient:
        """A client for the user the session belongs to (or an explicit user)."""
        from app.services.user_settings import ai_overrides, user_keys

        return cls(overrides=ai_overrides(db, user_id), keys=user_keys(db, user_id),
                   user_id=user_id if user_id is not None else db.info.get("user_id"))

    def _config(self) -> dict[str, Any]:
        return resolve_ai_config(self.settings, self.overrides)

    def _timeout(self) -> int:
        return int(getattr(self.settings, "llm_request_timeout_seconds", 120) or 120)

    def _retries(self) -> int:
        return int(getattr(self.settings, "llm_max_retries", 1) or 0)

    def generate_prompt_json(self, prompt: Prompt, call: LLMCall | None = None) -> dict[str, Any]:
        """Run a prompt built in app.prompts and return its JSON result."""
        return self.generate_prompt(prompt, call).data

    def generate_prompt(self, prompt: Prompt, call: LLMCall | None = None) -> LLMResult:
        """Run a prompt built in app.prompts. The stage comes from the prompt's marker, never from its data."""
        return self._generate(prompt.system, prompt.user, MARKER_TASKS.get(prompt.marker, "default"),
                              prompt_name=prompt.name, prompt_version=prompt.version, call=call)

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        return self._generate(system, user, _task_from_prompt(user)).data

    def model_for(self, task: str) -> tuple[str, str]:
        """The provider and model a stage would use."""
        config = self._config()
        return config["llm_provider"], config["stage_models"].get(task) or config["model"]

    def _generate(self, system: str, user: str, task: str, *, prompt_name: str | None = None,
                  prompt_version: str | None = None, call: LLMCall | None = None) -> LLMResult:
        config = self._config()
        provider = config["llm_provider"]
        model = config["stage_models"].get(task) or config["model"]
        attempt = Attempt(provider=provider, requested_model=model, user_id=self.user_id, call=call or LLMCall(),
                          prompt_name=prompt_name, prompt_version=prompt_version, system=system, user=user)
        started = time.monotonic()
        failure: BaseException | None = None
        failure_type: str | None = None
        try:
            result = self._dispatch(provider, system, user, task, model, config, attempt)
            if attempt.call.validate is not None:
                try:
                    validated = attempt.call.validate(result.data)
                except Exception as exc:
                    attempt.structured_output_valid = False
                    failure, failure_type = exc, error_type(exc, validation=True)
                    raise
                attempt.structured_output_valid = True
                result = LLMResult(result.data, result.provider, result.model, result.usage, validated)
            return result
        except Exception as exc:
            if failure is None:
                failure, failure_type = exc, error_type(exc)
                if isinstance(exc, LLMResponseError):
                    attempt.structured_output_valid = False
            raise
        finally:
            llm_telemetry.record(attempt, latency_ms=int((time.monotonic() - started) * 1000), error=failure,
                                 error_type=failure_type)

    def _dispatch(self, provider: str, system: str, user: str, task: str, model: str, config: dict[str, Any],
                  attempt: Attempt) -> LLMResult:
        if provider == "mock":
            attempt.sent, attempt.model = True, "mock"
            data = self._mock(task, user)
            attempt.response_text = json.dumps(data)
            return LLMResult(data, "mock", "mock")
        if provider == "openai":
            return self._openai(system, user, model, attempt)
        if provider == "anthropic":
            return self._anthropic(system, user, model, attempt)
        if provider == "gemini":
            return self._gemini(system, user, model, attempt)
        if provider == "openrouter":
            return self._openrouter(system, user, task=task, model=model, require_free=config["require_free"],
                                    attempt=attempt)
        raise LLMConfigError(f"Unsupported LLM_PROVIDER={provider}")

    @staticmethod
    def _openai_usage(response: Any) -> Usage | None:
        usage = getattr(response, "usage", None)
        prompt, completion = _int(getattr(usage, "prompt_tokens", None)), _int(getattr(usage, "completion_tokens", None))
        if usage is None or prompt is None:
            return None
        cached = _int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None))
        extra = getattr(usage, "model_extra", None) or {}
        cost = getattr(usage, "cost", None) if getattr(usage, "cost", None) is not None else extra.get("cost")
        return Usage(input_tokens=prompt, output_tokens=completion, cache_read_tokens=cached,
                     uncached_input_tokens=prompt - (cached or 0),
                     reported_cost=float(cost) if isinstance(cost, (int, float)) else None)

    def _openai(self, system: str, user: str, model: str, attempt: Attempt) -> LLMResult:
        if not self.keys.get("openai"):
            raise LLMConfigError("OPENAI_API_KEY is missing: add your OpenAI API key in Settings")
        from openai import OpenAI

        client = OpenAI(api_key=self.keys["openai"], timeout=self._timeout(), max_retries=self._retries())
        attempt.sent = True
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise _provider_error("OpenAI", model, exc) from exc
        attempt.model, attempt.usage = _answered_model(response, model), self._openai_usage(response)
        if attempt.usage:
            attempt.usage.reported_cost = None  # OpenAI does not report cost; the catalog prices it
        attempt.response_text = response.choices[0].message.content or "{}"
        return LLMResult(_extract_json(attempt.response_text), "openai", attempt.model, attempt.usage)

    def _anthropic(self, system: str, user: str, model: str, attempt: Attempt) -> LLMResult:
        if not self.keys.get("anthropic"):
            raise LLMConfigError("ANTHROPIC_API_KEY is missing: add your Anthropic API key in Settings")
        from anthropic import Anthropic

        client = Anthropic(api_key=self.keys["anthropic"], timeout=self._timeout(), max_retries=self._retries())
        attempt.sent = True
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=5000,
                system=system + "\nReturn only valid JSON. No markdown fences.",
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise _provider_error("Anthropic", model, exc) from exc
        attempt.model = _answered_model(msg, model)
        usage = getattr(msg, "usage", None)
        uncached = _int(getattr(usage, "input_tokens", None))
        if uncached is not None:
            read = _int(getattr(usage, "cache_read_input_tokens", None))
            write = _int(getattr(usage, "cache_creation_input_tokens", None))
            attempt.usage = Usage(input_tokens=uncached + (read or 0) + (write or 0),
                                  output_tokens=_int(getattr(usage, "output_tokens", None)), cache_read_tokens=read,
                                  cache_write_tokens=write, uncached_input_tokens=uncached)
        attempt.response_text = "".join(getattr(block, "text", "") for block in msg.content)
        return LLMResult(_extract_json(attempt.response_text), "anthropic", attempt.model, attempt.usage)

    def _gemini(self, system: str, user: str, model: str, attempt: Attempt) -> LLMResult:
        if not self.keys.get("gemini"):
            raise LLMConfigError("GEMINI_API_KEY is missing: add your Gemini API key in Settings")
        from google import genai

        client = genai.Client(api_key=self.keys["gemini"], http_options={"timeout": self._timeout() * 1000})
        attempt.sent = True
        try:
            response = client.models.generate_content(
                model=model, contents=user,
                config={"system_instruction": system, "response_mime_type": "application/json"})
        except Exception as exc:
            raise _provider_error("Gemini", model, exc) from exc
        version = getattr(response, "model_version", None)
        attempt.model = version if isinstance(version, str) and version else model
        meta = getattr(response, "usage_metadata", None)
        prompt = _int(getattr(meta, "prompt_token_count", None))
        if prompt is not None:
            cached = _int(getattr(meta, "cached_content_token_count", None))
            attempt.usage = Usage(input_tokens=prompt, output_tokens=_int(getattr(meta, "candidates_token_count", None)),
                                  cache_read_tokens=cached, uncached_input_tokens=prompt - (cached or 0))
        attempt.response_text = getattr(response, "text", None) or "{}"
        return LLMResult(_extract_json(attempt.response_text), "gemini", attempt.model, attempt.usage)

    def _openrouter(self, system: str, user: str, task: str, model: str, require_free: bool,
                    attempt: Attempt) -> LLMResult:
        if not self.keys.get("openrouter"):
            raise LLMConfigError("OPENROUTER_API_KEY is missing: add your OpenRouter API key in Settings")

        primary = model
        fallbacks = _csv_models(self.settings.openrouter_fallback_models)
        all_models = [primary, *fallbacks]

        # Avoid accidental spend when the user intends to run on free inference. Checked before any request.
        if require_free:
            paid = [name for name in all_models if not _is_free_openrouter_model(name)]
            if paid:
                raise LLMConfigError(
                    "OPENROUTER_REQUIRE_FREE=true but paid/non-free model(s) were configured: "
                    + ", ".join(paid)
                    + ". Use openrouter/free or a :free model, or turn off the free-only guard in Settings."
                )

        from openai import OpenAI

        headers: dict[str, str] = {"X-Title": self.settings.openrouter_app_name}
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(
            api_key=self.keys["openrouter"],
            base_url=self.settings.openrouter_base_url,
            default_headers=headers,
            timeout=self._timeout(),
            max_retries=self._retries(),
        )

        # OpenRouter extensions: model-level fallbacks, and usage accounting so each response reports its cost.
        extra_body: dict[str, Any] = {"usage": {"include": True}}
        if fallbacks:
            extra_body["models"] = fallbacks

        request_kwargs: dict[str, Any] = {
            "model": primary,
            "messages": [
                {"role": "system", "content": system + "\nReturn only valid JSON. No markdown fences."},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.openrouter_max_tokens,
            "extra_body": extra_body,
        }

        attempt.sent = True
        try:
            response = client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            raise _provider_error(f"OpenRouter (task={task})", primary, exc) from exc

        attempt.model, attempt.usage = _answered_model(response, primary), self._openai_usage(response)
        attempt.response_text = response.choices[0].message.content or "{}"
        return LLMResult(_extract_json(attempt.response_text), "openrouter", attempt.model, attempt.usage)

    def _mock(self, task: str, user: str) -> dict[str, Any]:
        # Useful for local smoke tests without API spend.
        if task == "rank":
            job_id = re.search(r"^JOB ID: (\d+)$", user, re.M)
            return {
                "job_id": int(job_id.group(1)) if job_id else 0,
                "overall_fit_score": 80,
                "confidence": 0.8,
                "candidate_fit_score": 82,
                "resume_fit_score": 74,
                "seniority_match_score": 80,
                "skills_match_score": 80,
                "leadership_match_score": 70,
                "domain_match_score": 75,
                "location_match_score": 90,
                "strong_matches": ["Python", "backend systems"],
                "partial_matches": ["Cloud infrastructure"],
                "missing_requirements": [],
                "fatal_gaps": [],
                "resume_gaps": ["Production ownership is not quantified on the resume"],
                "relevant_evidence_ids": [],
                "recommended_resume_focus": ["Lead with production ownership and measurable outcomes"],
                "qualification_recommendation": "QUALIFY",
                "reasoning_summary": "Good overall fit based on the supplied profile.",
            }
        if task == "review":
            return {
                "score": 86,
                "reject_reasons": [],
                "strengths": ["Relevant evidence", "Clear outcomes"],
                "improvements": ["Keep bullets concise"],
            }
        if task == "followup":
            return {"draft": "Hi, I wanted to follow up on my application and reiterate my interest in the role. Best regards."}
        if task == "advisor":
            return {"summary": "Mock analysis of the supplied usage aggregates.", "recommendations": [],
                    "estimated_savings": "unknown"}
        if "EXTRACT_EVIDENCE" in user.split("\n", 1)[0]:
            lines = [line.strip(" -•*\t") for line in user.split("MASTER RESUME:", 1)[-1].splitlines()]
            claims = [line for line in lines if len(line) > 25 and re.search(r"\d", line)][:12]
            return {"claims": [{"claim": c, "company": "", "role": "", "skills": []} for c in claims]}
        return {
            "summary": "Tailored engineering leader with production AI and backend experience.",
            "skills": ["Python", "FastAPI", "AWS", "Kubernetes"],
            "experience": [{"role_index": 0, "bullets": ["Built and operated production backend and AI systems."]}],
            "experience_bullets": ["Built and operated production backend and AI systems."],
            "selected_evidence_ids": [],
            "cover_letter": "I am excited to apply because the role aligns with my production engineering and AI experience.",
        }
