"""An in-process LiteLLM proxy (and Ollama tag list) for tests, served through httpx.MockTransport.

It implements only the LiteLLM 1.102 routes Job Hunt OS uses, with the same shapes the real proxy returns
(checked against a running 1.102.1 proxy): /health/readiness/details, /v1/models, /model/info, /router/settings,
/health/latest, /v1/chat/completions with x-litellm-* headers, /model/new|delete|{id}/update (refused unless
STORE_MODEL_IN_DB), /auto_router/validate_complexity_router_config and /spend/logs.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx

BASE = "http://litellm.test"
OLLAMA = "http://ollama.test:11434"
MASTER_KEY = "sk-test-master-key-123456"


def deployment(name: str, model: str, *, model_id: str | None = None, info: dict[str, Any] | None = None,
               **params: Any) -> dict[str, Any]:
    return {"model_name": name, "litellm_params": {"model": model, **params},
            "model_info": {"id": model_id or uuid.uuid4().hex, "db_model": False, **(info or {})}}


class FakeLiteLLM:
    def __init__(self):
        self.deployments = [
            deployment("tier-1-fast", "ollama_chat/llama3.2:3b", api_base=OLLAMA, num_ctx=8192, keep_alive="15m"),
            deployment("tier-2-reasoning", "ollama_chat/gpt-oss:20b", api_base=OLLAMA, num_ctx=24576),
            deployment("tier-3-review", "ollama_chat/qwen2.5:32b", api_base=OLLAMA, num_ctx=24576),
            deployment("cloud-mini", "openrouter/some/cloud-model", info={"max_input_tokens": 128000,
                                                                          "supports_response_schema": True}),
        ]
        self.ollama = {"llama3.2:3b": (131072, ["completion", "tools"]),
                       "gpt-oss:20b": (131072, ["completion", "tools", "thinking"]),
                       "qwen2.5:32b": (32768, ["completion", "tools"])}
        self.router_settings: dict[str, Any] = {"routing_strategy": "simple-shuffle", "fallbacks": []}
        self.store_model_in_db = True
        self.fail_new_model_at: int | None = None  # 1-based index of the /model/new call that fails
        self.new_model_calls = 0
        self.reject_router: str | None = None
        self.down = False
        self.unavailable: set[str] = set()  # model groups that fail (503) unless the request names fallbacks
        self.rate_limited: set[str] = set()
        self.answers: dict[str, list[Any]] = {}  # model → queued answers (dict → JSON content, str → raw content)
        self.route_tier = "SIMPLE"
        self.spend: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.chat_bodies: list[dict[str, Any]] = []
        self.hide_new_models = False  # accept /model/new but never show the result (verification failure)

    # ───────────── helpers ─────────────

    def names(self) -> list[str]:
        return sorted({d["model_name"] for d in self.deployments})

    def answer(self, model: str, *values: Any) -> None:
        self.answers.setdefault(model, []).extend(values)

    def managed(self) -> list[dict[str, Any]]:
        return [d for d in self.deployments if d["model_info"].get("jobhunt_managed")]

    def _json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode(), headers={
            "content-type": "application/json", **(headers or {})})

    def _error(self, status: int, message: str) -> httpx.Response:
        return self._json(status, {"error": {"message": message, "type": "error", "code": str(status)}})

    # ───────────── the proxy ─────────────

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        if request.url.host == "ollama.test":
            if path == "/api/tags":
                return self._json(200, {"models": [
                    {"name": name, "details": {"context_length": ctx, "family": name.split(":")[0]},
                     "capabilities": caps} for name, (ctx, caps) in self.ollama.items()]})
            return self._error(404, "not found")
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if path in ("/health/readiness", "/health/readiness/details"):
            return self._json(200, {"status": "healthy", "db": "connected", "litellm_version": "1.102.1"})
        if request.headers.get("authorization") != f"Bearer {MASTER_KEY}":
            return self._error(401, "Authentication Error, No api key passed in.")
        routes: dict[tuple[str, str], Callable[[Any], httpx.Response]] = {
            ("GET", "/v1/models"): lambda _: self._json(200, {"data": [{"id": n, "object": "model"}
                                                                       for n in self.names()], "object": "list"}),
            ("GET", "/model/info"): lambda _: self._json(200, {"data": self.deployments}),
            ("GET", "/router/settings"): lambda _: self._json(200, {"current_values": self.router_settings,
                                                                    "fields": []}),
            ("GET", "/health/latest"): lambda _: self._error(404, "not found"),
            ("POST", "/v1/chat/completions"): self._chat,
            ("POST", "/model/new"): self._new,
            ("POST", "/model/delete"): self._delete,
            ("POST", "/auto_router/validate_complexity_router_config"): self._validate,
            ("GET", "/spend/logs"): lambda _: self._json(200, [self.spend[request.url.params.get("request_id")]]
                                                         if request.url.params.get("request_id") in self.spend
                                                         else []),
        }
        handler = routes.get((request.method, path))
        if handler is None:
            return self._error(404, f"{request.method} {path} not found")
        return handler(body)

    def _new(self, body: dict[str, Any]) -> httpx.Response:
        if not self.store_model_in_db:
            return self._error(500, "Set `'STORE_MODEL_IN_DB='True'` in your env to enable this feature.")
        self.new_model_calls += 1
        if self.fail_new_model_at == self.new_model_calls:
            return self._error(400, f"Invalid litellm_params for {body['model_name']}")
        model_id = uuid.uuid4().hex
        if not self.hide_new_models:
            self.deployments.append({"model_name": body["model_name"], "litellm_params": body["litellm_params"],
                                     "model_info": {**body["model_info"], "id": model_id, "db_model": True}})
        return self._json(200, {"model_id": model_id, "model_info": {"id": model_id}})

    def _delete(self, body: dict[str, Any]) -> httpx.Response:
        if not self.store_model_in_db:
            return self._error(500, "Set `'STORE_MODEL_IN_DB='True'` in your env to enable this feature.")
        before = len(self.deployments)
        self.deployments = [d for d in self.deployments if d["model_info"]["id"] != body["id"]]
        return self._json(200 if len(self.deployments) < before else 404, {"message": "deleted"})

    def _validate(self, body: dict[str, Any]) -> httpx.Response:
        if self.reject_router:
            return self._error(400, self.reject_router)
        return self._json(200, {"valid": True})

    def _chat(self, body: dict[str, Any]) -> httpx.Response:
        self.chat_bodies.append(body)
        requested = body["model"]
        group = requested
        by_name = {d["model_name"]: d for d in self.deployments}
        router = by_name.get(requested)
        if router and str(router["litellm_params"].get("model", "")).startswith("auto_router/"):
            group = router["litellm_params"]["complexity_router_config"]["tiers"][self.route_tier]
        attempted = 0
        if group in self.rate_limited:
            return self._error(429, "Rate limit reached")
        if group in self.unavailable:
            fallbacks = body.get("fallbacks") or []
            if not fallbacks:
                return self._error(503, f"{group} is unavailable")
            group, attempted = fallbacks[0], 1
        if group not in by_name:
            return self._error(400, f"Invalid model name passed in model={group}")
        queued = self.answers.get(group) or []
        value = queued.pop(0) if queued else {"ok": True, "confidence": 0.9}
        content = value if isinstance(value, str) else json.dumps(value)
        call_id = str(uuid.uuid4())
        underlying = by_name[group]["litellm_params"]["model"]
        return self._json(200, {"id": "chatcmpl-1", "model": group, "object": "chat.completion",
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": content}}],
                                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}},
                          headers={"x-litellm-call-id": call_id, "x-litellm-model-group": group,
                                   "x-litellm-model-name": underlying, "x-litellm-model-id": "dep-" + group,
                                   "x-litellm-version": "1.102.1", "x-litellm-response-cost": "0.0",
                                   "x-litellm-attempted-fallbacks": str(attempted),
                                   "x-litellm-attempted-retries": "0"})


# ───────────── fixtures (import them into a test module to use them) ─────────────

import pytest

from app.services import litellm_client, litellm_config_manager, model_resolution
from app.settings import get_settings

GATEWAY_SETTINGS = {"litellm_base_url": BASE, "litellm_api_key": None, "litellm_tier_fast": "tier-1-fast",
                    "litellm_tier_primary": "tier-2-reasoning", "litellm_tier_deep": "tier-3-review",
                    "litellm_auto_router_model": None, "litellm_sync_mode": "admin_api", "ollama_base_url": OLLAMA,
                    "litellm_default_model": None, "ai_config_cache_seconds": 30, "litellm_managed_config_path": None,
                    "litellm_config_path": None, "litellm_restart_command": None, "rank_model": None,
                    "tailor_model": None, "review_model": None, "followup_model": None}


@pytest.fixture
def litellm(monkeypatch):
    fake = FakeLiteLLM()
    litellm_client.use_transport(httpx.MockTransport(fake))
    settings = get_settings()
    for name, value in GATEWAY_SETTINGS.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(litellm_config_manager, "VERIFY_WAIT_SECONDS", 0)
    model_resolution.invalidate()
    yield fake
    litellm_client.use_transport(None)
    model_resolution.invalidate()


class Account:
    """A freshly signed-up account with its LiteLLM key saved, driving the real API."""

    def __init__(self, client: Any, user_id: int, email: str):
        self.client, self.user_id, self.email = client, user_id, email

    def get(self, path: str, **kw: Any) -> Any:
        response = self.client.get(f"/api/settings/ai{path}", **kw)
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, method: str, path: str, expect: int = 200, **kw: Any) -> Any:
        response = self.client.request(method, f"/api/settings/ai{path}", **kw)
        assert response.status_code == expect, response.text
        return response.json()

    def revision(self) -> int:
        return self.get("")["connection"]["draft_revision"]

    def worker(self, worker_type: str, **changes: Any) -> Any:
        return self.call("PUT", f"/workers/{worker_type}", json={"revision": self.revision(), **changes})

    def apply(self, expect: int = 200) -> Any:
        preview = self.call("POST", "/preview")
        return self.call("POST", "/apply", expect=expect,
                         json={"revision": preview["draft_revision"], "base_version": preview["base_version"]})

    def gateway(self, enabled: bool = True) -> Any:
        return self.call("PUT", "/gateway", json={"enabled": enabled})


@pytest.fixture
def account(anonymous, litellm):
    import uuid as _uuid

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    email = f"ai-{_uuid.uuid4().hex}@example.com"
    result = client.post("/api/auth/signup", json={"email": email, "password": "a password for tests",
                                                   "confirm_password": "a password for tests"})
    assert result.status_code == 201, result.text
    acct = Account(client, result.json()["user"]["id"], email)
    acct.call("PUT", "/connection", json={"api_key": MASTER_KEY})
    return acct
