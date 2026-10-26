"""The LiteLLM proxy's HTTP API, as Job Hunt OS uses it. Every call goes through the instrumented client.

Only endpoints that exist in LiteLLM 1.102 (checked against its OpenAPI schema) are used:

    GET  /health/readiness/details   version and database state (no model calls)
    GET  /health/readiness           the same, for proxies without the details route
    GET  /v1/models                  model names the key may use
    GET  /model/info                 deployments: litellm_params (without credentials) and model_info
    GET  /router/settings            router settings as the proxy runs them
    GET  /health/latest              results of LiteLLM's own background health checks, when it runs them
    POST /v1/chat/completions        inference, and the small model test
    POST /v1/embeddings              embeddings (app.services.embeddings), and the embedding model test
    POST /model/new                  create a deployment        ─┐ these three require STORE_MODEL_IN_DB=True
    PATCH /model/{id}/update         change a deployment         │ on the proxy; LiteLLM answers otherwise with
    POST /model/delete               remove a deployment        ─┘ an error naming that setting
    POST /auto_router/validate_complexity_router_config   validate an auto router config without saving it

Nothing here decides anything: callers get typed errors (unreachable, unauthorised, rejected, unsupported) and
decide what they mean. The API key is sent as a bearer token and is never logged; the instrumentation layer
redacts the Authorization header wholesale.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from app.services.external_api import NO_CAPTURE, RateLimitedCooldown, external_client
from app.services.redaction import redact_text, redact_value
from app.settings import get_settings

PROVIDER = "LITELLM"
# Tests replace the network with an in-process LiteLLM (httpx.MockTransport); the instrumented client is kept, so
# telemetry is exercised exactly as in production.
_transport: httpx.BaseTransport | None = None
# litellm_params keys that may hold a credential; they are dropped from anything Job Hunt OS stores or shows.
CREDENTIAL_PARAMS = frozenset({"api_key", "aws_secret_access_key", "aws_access_key_id", "aws_session_token",
                               "vertex_credentials", "azure_ad_token", "client_secret", "litellm_credential_name",
                               "api_version_key", "token", "password", "master_key"})


class LiteLLMError(RuntimeError):
    """A LiteLLM management call failed."""

    status: str = "UNAVAILABLE"

    def __init__(self, message: str, *, http_status: int | None = None, rate_limit: Any = None):
        super().__init__(redact_text(message, 500))
        self.http_status = http_status
        self.rate_limit = rate_limit  # a 429's reading (app.services.rate_limits), made before the text is cut


class LiteLLMUnavailable(LiteLLMError):
    status = "UNAVAILABLE"


class LiteLLMAuthError(LiteLLMError):
    status = "AUTH_FAILED"


class LiteLLMRejected(LiteLLMError):
    """LiteLLM understood the request and refused it (validation, STORE_MODEL_IN_DB off, unknown model…)."""

    status = "REJECTED"


class LiteLLMUnsupported(LiteLLMError):
    """The route does not exist on this LiteLLM version."""

    status = "UNSUPPORTED"


def use_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def normalize_base_url(value: str | None) -> str | None:
    """The proxy's base URL without a trailing slash or a trailing /v1. None when it is not an http(s) URL."""
    clean = (value or "").strip().rstrip("/")
    clean = clean.removesuffix("/v1")
    parts = urlsplit(clean)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return clean


def without_credentials(params: dict[str, Any] | None) -> dict[str, Any]:
    """litellm_params with every credential-shaped key removed, and the rest redacted."""
    clean = {k: v for k, v in (params or {}).items() if k.lower() not in CREDENTIAL_PARAMS and "secret" not in k.lower()}
    return redact_value(clean)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    detail = body.get("error") or body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error") or detail
    return str(detail)[:300]


@dataclass
class ChatResult:
    body: dict[str, Any]
    headers: dict[str, str]
    latency_ms: int
    request_id: str | None = None  # the generic external_api_request_logs row
    litellm: dict[str, Any] = field(default_factory=dict)  # facts read from x-litellm-* headers


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str | None  # the model the answer names (what answered), when it names one
    input_tokens: int | None
    latency_ms: int
    request_id: str | None = None
    litellm: dict[str, Any] = field(default_factory=dict)


def litellm_headers(headers: Any) -> dict[str, Any]:
    """The routing facts LiteLLM puts on every response. Absent headers are absent, never guessed."""
    def get(name: str) -> str | None:
        value = headers.get(name) if headers is not None else None
        return value if value not in (None, "", "None") else None

    def number(name: str, kind: type) -> Any:
        value = get(name)
        try:
            return kind(float(value)) if value is not None else None
        except ValueError:
            return None

    return {k: v for k, v in {
        "call_id": get("x-litellm-call-id"), "model_id": get("x-litellm-model-id"),
        "model_name": get("x-litellm-model-name"), "model_group": get("x-litellm-model-group"),
        "api_base": get("x-litellm-model-api-base"), "version": get("x-litellm-version"),
        "attempted_fallbacks": number("x-litellm-attempted-fallbacks", int),
        "attempted_retries": number("x-litellm-attempted-retries", int),
        "response_cost": number("x-litellm-response-cost", float)
        if get("x-litellm-response-cost") is not None else number("x-litellm-response-cost-original", float),
        "response_duration_ms": number("x-litellm-response-duration-ms", float),
        "overhead_duration_ms": number("x-litellm-overhead-duration-ms", float),
    }.items() if v is not None}


class LiteLLMAPI:
    """One conversation with a LiteLLM proxy. Use as a context manager so its connection is closed."""

    def __init__(self, base_url: str, api_key: str | None, *, user_id: int | None = None,
                 timeout: float | None = None, service: str = "admin"):
        normalized = normalize_base_url(base_url)
        if normalized is None:
            raise LiteLLMRejected("The LiteLLM base URL must be an http(s) URL")
        self.base_url = normalized
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        extra = {"transport": _transport} if _transport is not None else {}
        self.client = external_client(PROVIDER, service=service, operation="request", user_id=user_id,
                                      timeout=timeout or get_settings().litellm_admin_timeout_seconds,
                                      headers=headers, base_url=normalized, **extra)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.client.close()

    def _send(self, method: str, path: str, operation: str, *, json: Any = None, params: dict | None = None,
              metadata: dict[str, Any] | None = None, timeout: float | None = None) -> httpx.Response:
        extensions: dict[str, Any] = {"operation": operation}
        if metadata:
            extensions["metadata"] = metadata
        try:
            response = self.client.request(method, path, json=json, params=params, extensions=extensions,
                                           **({"timeout": timeout} if timeout else {}))
        except RateLimitedCooldown:
            raise  # the account's cooldown for LiteLLM is running: the caller reports it as a rate limit
        except httpx.TimeoutException as exc:
            raise LiteLLMUnavailable(f"LiteLLM did not answer in time ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise LiteLLMUnavailable(f"LiteLLM is unreachable at {self.base_url}: {type(exc).__name__}") from exc
        self._check(response, method, path)
        return response

    @staticmethod
    def _check(response: httpx.Response, method: str, path: str) -> None:
        if response.status_code in (401, 403):
            raise LiteLLMAuthError(f"LiteLLM refused the API key: {_error_message(response)}",
                                   http_status=response.status_code)
        if response.status_code == 404:
            raise LiteLLMUnsupported(f"{method} {path} is not available on this LiteLLM", http_status=404)
        if response.status_code >= 500 and "STORE_MODEL_IN_DB" not in response.text:
            raise LiteLLMUnavailable(f"LiteLLM failed ({response.status_code}): {_error_message(response)}",
                                     http_status=response.status_code)
        if response.status_code == 429:
            from app.services import rate_limits

            raise LiteLLMRejected(_error_message(response), http_status=429,
                                  rate_limit=rate_limits.classify(response.text, response.headers))
        if response.status_code >= 400:
            raise LiteLLMRejected(_error_message(response), http_status=response.status_code)

    def _json(self, method: str, path: str, operation: str, **kwargs: Any) -> Any:
        response = self._send(method, path, operation, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise LiteLLMError(f"LiteLLM returned a non-JSON answer for {path}") from exc

    # ───────────── reads ─────────────

    def readiness(self) -> dict[str, Any]:
        try:
            return self._json("GET", "/health/readiness/details", "readiness")
        except LiteLLMUnsupported:
            return self._json("GET", "/health/readiness", "readiness")

    def model_names(self) -> list[str]:
        data = self._json("GET", "/v1/models", "list_models").get("data") or []
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]

    def model_info(self) -> list[dict[str, Any]]:
        """Deployments, with credentials removed from litellm_params before anything else sees them."""
        data = self._json("GET", "/model/info", "model_info").get("data") or []
        return [{"model_name": item.get("model_name"),
                 "litellm_params": without_credentials(item.get("litellm_params")),
                 "model_info": redact_value(item.get("model_info") or {})}
                for item in data if isinstance(item, dict) and item.get("model_name")]

    def router_settings(self) -> dict[str, Any]:
        return self._json("GET", "/router/settings", "router_settings").get("current_values") or {}

    def health_latest(self) -> dict[str, Any] | None:
        """LiteLLM's latest background health checks, or None when this proxy does not run them."""
        try:
            return self._json("GET", "/health/latest", "health_latest")
        except (LiteLLMUnsupported, LiteLLMRejected):
            return None

    def spend_log(self, call_id: str) -> dict[str, Any] | None:
        """LiteLLM's own log row for one request (its x-litellm-call-id), or None when it has none (spend logs
        off, not flushed yet, or pruned)."""
        body = self._json("GET", "/spend/logs", "spend_log", params={"request_id": call_id})
        rows = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else None) or []
        return next((r for r in rows if isinstance(r, dict)), None)

    # ───────────── inference ─────────────

    def chat(self, body: dict[str, Any], *, operation: str = "chat_completion", timeout: float | None = None,
             metadata: dict[str, Any] | None = None) -> ChatResult:
        started = time.monotonic()
        response = self._send("POST", "/v1/chat/completions", operation, json=body, metadata=metadata,
                              timeout=timeout)
        latency = int((time.monotonic() - started) * 1000)
        try:
            parsed = response.json()
        except ValueError as exc:
            raise LiteLLMError("LiteLLM returned a non-JSON completion") from exc
        return ChatResult(parsed, dict(response.headers), latency, response.extensions.get("jobhunt_request_id"),
                          litellm_headers(response.headers))

    def embeddings(self, model: str, inputs: list[str], *, operation: str = "embed_documents",
                   timeout: float | None = None, metadata: dict[str, Any] | None = None) -> EmbeddingResult:
        """Vectors for `inputs`, in order. The cost LiteLLM reports (x-litellm-response-cost) is recorded on the call's
        telemetry row; without it the cost stays unknown (NULL). Inputs and vectors are never logged."""
        def report(response: httpx.Response, call: Any) -> None:
            facts = litellm_headers(response.headers)
            call.note(answered_model=facts.get("model_name"), model_group=facts.get("model_group"),
                      inputs=len(inputs))
            if response.status_code < 400:
                call.record_usage(cost=facts.get("response_cost"), source="provider_reported")

        started = time.monotonic()
        # NO_CAPTURE: the inputs are candidate evidence and the answer is vectors, so neither is kept even when an
        # account's payload capture or EXTERNAL_API_DEBUG_PAYLOADS is on; sizes, status and cost are still recorded.
        extensions: dict[str, Any] = {"operation": operation, "on_response": report, NO_CAPTURE: True,
                                      "metadata": {"model": model, **(metadata or {})}}
        try:
            response = self.client.request("POST", "/v1/embeddings", json={"model": model, "input": inputs},
                                           extensions=extensions, **({"timeout": timeout} if timeout else {}))
        except RateLimitedCooldown:
            raise
        except httpx.TimeoutException as exc:
            raise LiteLLMUnavailable(f"LiteLLM did not answer in time ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise LiteLLMUnavailable(f"LiteLLM is unreachable at {self.base_url}: {type(exc).__name__}") from exc
        self._check(response, "POST", "/v1/embeddings")
        latency = int((time.monotonic() - started) * 1000)
        try:
            parsed = response.json()
        except ValueError as exc:
            raise LiteLLMError("LiteLLM returned a non-JSON embedding answer") from exc
        rows = sorted((r for r in (parsed.get("data") or []) if isinstance(r, dict)),
                      key=lambda r: r.get("index", 0))
        vectors = [[float(x) for x in (r.get("embedding") or [])] for r in rows]
        if len(vectors) != len(inputs):
            raise LiteLLMError(f"LiteLLM returned {len(vectors)} vectors for {len(inputs)} inputs")
        usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
        return EmbeddingResult(vectors=vectors, model=str(parsed.get("model") or "") or None,
                               input_tokens=usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int)
                               else None, latency_ms=latency, request_id=response.extensions.get("jobhunt_request_id"),
                               litellm=litellm_headers(response.headers))

    # ───────────── writes (STORE_MODEL_IN_DB) ─────────────

    def new_model(self, model_name: str, litellm_params: dict[str, Any], model_info: dict[str, Any]) -> str | None:
        body = self._json("POST", "/model/new", "model_new", json={
            "model_name": model_name, "litellm_params": litellm_params, "model_info": model_info},
            metadata={"model_name": model_name})
        info = body.get("model_info") if isinstance(body, dict) else None
        return (info or {}).get("id") or (body.get("model_id") if isinstance(body, dict) else None)

    def update_model(self, model_id: str, model_name: str, litellm_params: dict[str, Any],
                     model_info: dict[str, Any]) -> None:
        self._json("PATCH", f"/model/{model_id}/update", "model_update", json={
            "model_name": model_name, "litellm_params": litellm_params, "model_info": model_info},
            metadata={"model_name": model_name})

    def delete_model(self, model_id: str) -> None:
        self._json("POST", "/model/delete", "model_delete", json={"id": model_id}, metadata={"model_id": model_id})

    def validate_complexity_router(self, config: dict[str, Any]) -> str | None:
        """None when LiteLLM accepts the auto router config, else its reason. Raises LiteLLMUnsupported on
        versions without the validation route, so the caller can say the check was not possible."""
        try:
            body = self._json("POST", "/auto_router/validate_complexity_router_config", "validate_auto_router",
                              json={"complexity_router_config": config})
        except LiteLLMRejected as exc:
            return str(exc)
        if isinstance(body, dict) and body.get("valid") is False:
            return str(body.get("error") or body.get("errors") or "LiteLLM rejected the auto router config")[:500]
        return None


def ollama_tags(api_base: str, *, user_id: int | None = None) -> dict[str, dict[str, Any]]:
    """What a local Ollama server says about its models (context length, capabilities), keyed by model name.

    Discovery metadata only — inference never goes to Ollama directly when the LiteLLM gateway is on. Any failure
    returns {} and the facts stay unknown.
    """
    base = normalize_base_url(api_base)
    if base is None:
        return {}
    try:
        extra = {"transport": _transport} if _transport is not None else {}
        with external_client("OLLAMA", service="discovery", operation="tags", user_id=user_id,
                             timeout=get_settings().litellm_admin_timeout_seconds, **extra) as client:
            response = client.get(f"{base}/api/tags")
            response.raise_for_status()
            models = response.json().get("models") or []
    except (httpx.HTTPError, ValueError, AttributeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in models:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        details = item.get("details") or {}
        out[str(item["name"])] = {"context_length": details.get("context_length"),
                                  "capabilities": list(item.get("capabilities") or []),
                                  "family": details.get("family"), "parameter_size": details.get("parameter_size")}
    return out
