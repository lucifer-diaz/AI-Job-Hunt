"""LiteLLM gateway mode in the LLM layer: dynamic resolution, parameters, fallback, escalation, telemetry, errors."""
import threading

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ExternalAPIRequestLog, LLMRequestLog
from app.prompts.common import Prompt
from app.services import model_resolution
from app.services.ai_workers import WORKER_BY_PROMPT
from app.services.llm import (
    LLMCall,
    LLMClient,
    LLMConfigError,
    LLMContextOverflowError,
    LLMError,
    LLMRateLimitError,
)
from app.services.model_resolution import (
    ResolvedWorker,
    concurrency_slot,
    escalation_reason,
    request_params,
)
from app.settings import get_settings


def ranking_prompt(job_id: int = 1) -> Prompt:
    return Prompt("ranking", "v4", "FIT_ANALYSIS", "Score the job.", f"FIT_ANALYSIS\nJOB ID: {job_id}")


def run(account, prompt: Prompt, **call):
    with SessionLocal() as db:
        db.info["user_id"] = account.user_id
        return LLMClient.for_user(db).generate_prompt(prompt, LLMCall(**call))


def logs(account):
    with SessionLocal() as db:
        return db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == account.user_id)
                          .order_by(LLMRequestLog.id)).all()


@pytest.fixture
def gateway(account):
    account.call("POST", "/models/sync")
    account.apply()
    account.gateway(True)
    return account


def test_every_worker_prompt_maps_to_a_worker():
    from app import prompts

    for name in prompts.PROMPT_VERSIONS:
        if name.startswith("mcp_") or name in ("agents", "model_health", "worker_test"):
            continue
        assert name in WORKER_BY_PROMPT, f"prompt {name} has no worker, so its model cannot be configured"


def test_a_worker_uses_its_configured_model_and_settings_without_a_restart(gateway, litellm):
    result = run(gateway, ranking_prompt())
    body = litellm.chat_bodies[-1]
    assert body["model"] == "tier-2-reasoning" and result.provider == "litellm"
    assert result.model == "ollama_chat/gpt-oss:20b"  # what actually answered, from x-litellm-model-name
    assert body["response_format"] == {"type": "json_object"} and body["drop_params"] is True
    assert body["metadata"]["worker_type"] == "RANKER" and body["metadata"]["jobhunt_config_version"] == 2
    assert "temperature" not in body and "max_tokens" not in body  # not configured: the model's defaults

    gateway.worker("RANKER", model_mode="FIXED", primary_model="tier-3-review", temperature=0.2,
                   max_output_tokens=800, timeout_seconds=45, retry_count=2)
    run(gateway, ranking_prompt())
    assert litellm.chat_bodies[-1]["model"] == "tier-2-reasoning", "an edit is not live until it is applied"
    gateway.apply()
    run(gateway, ranking_prompt())
    body = litellm.chat_bodies[-1]
    assert body["model"] == "tier-3-review" and body["temperature"] == 0.2 and body["max_tokens"] == 800
    assert body["timeout"] == 45 and body["num_retries"] == 2
    assert body["metadata"]["jobhunt_config_version"] == 3


def test_applying_clears_the_cache_immediately(gateway, litellm, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_config_cache_seconds", 3600)
    model_resolution.invalidate()
    assert model_resolution.get(gateway.user_id, "RANKER").model == "tier-2-reasoning"
    with SessionLocal() as db:  # a change written behind the cache's back is not seen until the TTL ...
        from app.models import AIConfigVersion

        latest = db.scalar(select(AIConfigVersion).where(AIConfigVersion.user_id == gateway.user_id)
                           .order_by(AIConfigVersion.version_number.desc()))
        latest.configuration_snapshot = {**latest.configuration_snapshot, "workers": {
            **latest.configuration_snapshot["workers"], "RANKER": {"mode": "FIXED", "primary_model": "cloud-mini"}}}
        db.commit()
    assert model_resolution.get(gateway.user_id, "RANKER").model == "tier-2-reasoning"
    gateway.worker("TAILOR", temperature=0.5)
    gateway.apply()  # ... but an Apply invalidates it at once
    assert model_resolution.get(gateway.user_id, "RANKER").model == "tier-2-reasoning"
    assert model_resolution.get(gateway.user_id, "TAILOR").temperature == 0.5


def test_reasoning_effort_is_only_sent_to_models_known_to_reason():
    snapshot = {"models": {"thinker": {"supports_reasoning": True}, "plain": {"supports_reasoning": False},
                           "mystery": {}, "nojson": {"supports_structured_output": False}},
                "workers": {"RANKER": {"mode": "FIXED", "primary_model": "thinker", "reasoning_effort": "high",
                                       "fallback_model": "plain"}}}
    worker = model_resolution.resolve(snapshot, "RANKER")
    assert request_params(worker, "thinker")[0]["reasoning_effort"] == "high"
    params, withheld = request_params(worker, "plain")
    assert "reasoning_effort" not in params and withheld == ["reasoning_effort"]
    snapshot["workers"]["RANKER"]["primary_model"] = "nojson"
    worker = model_resolution.resolve(snapshot, "RANKER")
    params, withheld = request_params(worker, "nojson")
    assert "response_format" not in params and "response_format" in withheld


def test_reasoning_effort_reaches_litellm_for_a_reasoning_model(gateway, litellm):
    gateway.worker("RANKER", reasoning_effort="high")
    gateway.apply()
    run(gateway, ranking_prompt())
    assert litellm.chat_bodies[-1]["reasoning_effort"] == "high"


def test_the_fallback_model_is_litellms_client_side_fallback(gateway, litellm):
    gateway.worker("RANKER", fallback_model="tier-3-review")
    gateway.apply()
    litellm.unavailable.add("tier-2-reasoning")
    result = run(gateway, ranking_prompt())
    assert litellm.chat_bodies[-1]["fallbacks"] == ["tier-3-review"]
    assert result.routing["model_group"] == "tier-3-review" and result.routing["fallback_used"] is True
    row = logs(gateway)[-1]
    assert row.fallback_used is True and row.model_group == "tier-3-review" and row.escalated is False
    assert row.requested_model == "tier-2-reasoning" and row.model == "ollama_chat/qwen2.5:32b"


def test_low_confidence_escalates_once_to_the_escalation_model(gateway, litellm):
    gateway.worker("RANKER", fallback_model="tier-1-fast", escalation={
        "tier": "DEEP_REVIEW", "conditions": ["LOW_CONFIDENCE"], "confidence_threshold": 0.85})
    gateway.apply()
    litellm.answer("tier-2-reasoning", {"score": 60, "confidence": 0.5})
    litellm.answer("tier-3-review", {"score": 72, "confidence": 0.95})
    result = run(gateway, ranking_prompt(), workflow_type="job", entity_id=7)
    assert result.data == {"score": 72, "confidence": 0.95} and result.routing["escalation_reason"] == "LOW_CONFIDENCE"
    first, second = litellm.chat_bodies[-2:]
    assert first["model"] == "tier-2-reasoning" and second["model"] == "tier-3-review"
    assert "fallbacks" not in second, "an escalated request gets no silent fallback to a weaker model"
    assert second["metadata"]["escalation"] == "LOW_CONFIDENCE" and second["metadata"]["previous_confidence"] == 0.5
    assert second["metadata"]["job_id"] == 7 and second["metadata"]["confidence_required"] == 0.85
    rows = logs(gateway)[-2:]
    assert [(r.escalated, r.escalation_reason, r.confidence) for r in rows] == [
        (False, None, 0.5), (True, "LOW_CONFIDENCE", 0.95)]


def test_a_confident_answer_does_not_escalate(gateway, litellm):
    gateway.worker("RANKER", escalation={"tier": "DEEP_REVIEW", "conditions": ["LOW_CONFIDENCE"],
                                         "confidence_threshold": 0.85})
    gateway.apply()
    litellm.answer("tier-2-reasoning", {"confidence": 0.9})
    run(gateway, ranking_prompt())
    assert [b["model"] for b in litellm.chat_bodies] == ["tier-2-reasoning"]


def test_schema_failure_escalates_only_when_the_policy_says_so(gateway, litellm):
    def validate(data):
        if "score" not in data:
            raise ValueError("score missing")
        return data

    litellm.answer("tier-2-reasoning", {"nope": 1})
    with pytest.raises(ValueError):
        run(gateway, ranking_prompt(), validate=validate)
    gateway.worker("RANKER", escalation={"model": "tier-3-review", "conditions": ["SCHEMA_FAILURE"]})
    gateway.apply()
    litellm.answer("tier-2-reasoning", "this is not json")
    litellm.answer("tier-3-review", {"score": 80})
    result = run(gateway, ranking_prompt(), validate=validate)
    assert result.validated == {"score": 80} and result.routing["escalation_reason"] == "SCHEMA_FAILURE"
    first, second = logs(gateway)[-2:]
    assert first.error_type == "INVALID_JSON" and first.structured_output_valid is False
    assert second.escalated and second.structured_output_valid is True


def test_escalation_conditions_are_evaluated_centrally():
    worker = ResolvedWorker(worker_type="RANKER", mode="TIER", configured="PRIMARY", model="a",
                            escalation=model_resolution.Escalation("b", ("LOW_CONFIDENCE", "AMBIGUOUS", "MANUAL"),
                                                                   0.8))
    assert escalation_reason(worker, data={"confidence": 79}) == "LOW_CONFIDENCE"  # a 0–100 score is scaled
    assert escalation_reason(worker, data={"confidence": 0.81}) is None
    assert escalation_reason(worker, data={"confidence": 0.9, "ambiguous": True}) == "AMBIGUOUS"
    assert escalation_reason(worker, data={}, manual=True) == "MANUAL"
    assert escalation_reason(worker, schema_failed=True) is None  # not in this worker's policy


def test_auto_router_requests_record_the_tier_that_answered(gateway, litellm):
    gateway.call("PUT", "/routing", json={"revision": gateway.revision(), "auto_router_enabled": True,
                                          "classifier": "HEURISTIC", "auto_router_model_name": "jobhunt-auto",
                                          "manage_auto_router": True, "tier_models": {
                                              "SIMPLE": "tier-1-fast", "MEDIUM": "tier-2-reasoning",
                                              "COMPLEX": "tier-2-reasoning", "REASONING": "tier-3-review"}})
    out = gateway.apply()
    assert [op["op"] for op in out["sync"]["operations"]] == ["create"]
    litellm.route_tier = "REASONING"
    result = run(gateway, Prompt("inbox", "v1", "INBOX", "Classify.", "INBOX\nEMAIL SUBJECT: hi"))
    assert litellm.chat_bodies[-1]["model"] == "jobhunt-auto"
    assert result.routing["routing_tier"] == "REASONING" and result.routing["model_group"] == "tier-3-review"
    row = logs(gateway)[-1]
    assert (row.worker_type, row.model_mode, row.configured_model, row.requested_model, row.routing_tier) == (
        "INBOX_CLASSIFIER", "AUTO_ROUTER", "AUTO_ROUTER", "jobhunt-auto", "REASONING")
    assert row.litellm_call_id and row.model == "ollama_chat/qwen2.5:32b" and row.cost_source == "provider_reported"
    with SessionLocal() as db:
        external = db.scalar(select(ExternalAPIRequestLog).where(
            ExternalAPIRequestLog.request_id == row.external_request_id))
    assert external.provider == "LITELLM" and external.operation == "chat_completion"
    assert external.request_metadata["worker_type"] == "INBOX_CLASSIFIER"


def test_monitoring_shows_requested_and_actual_models_and_routing(gateway, litellm):
    gateway.worker("RANKER", escalation={"tier": "DEEP_REVIEW", "conditions": ["LOW_CONFIDENCE"],
                                         "confidence_threshold": 0.85})
    gateway.apply()
    litellm.answer("tier-2-reasoning", {"confidence": 0.4})
    run(gateway, ranking_prompt())
    client = gateway.client
    dash = client.get("/api/monitoring/dashboard?range=today").json()
    workers = {w["worker_type"]: w for w in dash["routing"]["workers"]}
    assert workers["RANKER"]["requests"] == 2 and workers["RANKER"]["escalations"] == 1
    assert workers["RANKER"]["escalation_rate"] == 1.0
    assert {m["model"] for m in dash["routing"]["models"]} >= {"ollama_chat/gpt-oss:20b", "ollama_chat/qwen2.5:32b"}
    assert dash["routing"]["escalations"] == [{"worker_type": "RANKER", "reason": "LOW_CONFIDENCE", "requests": 1}]
    assert "RANKER" in dash["options"]["worker_types"]
    listing = client.get("/api/monitoring/requests?range=today&worker_type=RANKER").json()
    assert listing["total"] == 2
    detail = client.get(f"/api/monitoring/requests/{listing['items'][0]['id']}").json()
    assert detail["worker_type"] == "RANKER" and detail["requested_model"] == "tier-3-review"
    assert detail["model"] == "ollama_chat/qwen2.5:32b" and detail["escalated"] is True
    assert detail["litellm_call_id"] and detail["external_request_id"] and detail["config_version"] == 3


def test_gateway_mode_never_calls_a_provider_directly(gateway, monkeypatch):
    import anthropic
    import openai

    def refuse(*_a, **_k):
        raise AssertionError("a direct provider SDK was used in gateway mode")

    monkeypatch.setattr(openai, "OpenAI", refuse)
    monkeypatch.setattr(anthropic, "Anthropic", refuse)
    assert run(gateway, ranking_prompt()).provider == "litellm"


@pytest.mark.parametrize("setup, error, error_type", [
    (lambda f: setattr(f, "down", True), LLMError, "PROVIDER_ERROR"),
    (lambda f: f.rate_limited.add("tier-2-reasoning"), LLMRateLimitError, "RATE_LIMIT"),
])
def test_litellm_failures_become_the_typed_errors_workers_retry_on(gateway, litellm, setup, error, error_type):
    setup(litellm)
    with pytest.raises(error):
        run(gateway, ranking_prompt())
    row = logs(gateway)[-1]
    assert row.status == "FAILED" and row.error_type == error_type and row.external_request_id


def test_a_rejected_key_or_unknown_model_is_a_configuration_error(gateway, litellm):
    gateway.call("PUT", "/connection", json={"api_key": "sk-wrong-key-000000"})
    with pytest.raises(LLMConfigError):
        run(gateway, ranking_prompt())
    assert logs(gateway)[-1].error_type == "CONFIGURATION"


def test_a_disabled_worker_or_missing_base_url_is_refused_before_sending(gateway, litellm, monkeypatch):
    gateway.worker("RANKER", enabled=False)
    gateway.apply()
    with pytest.raises(LLMConfigError, match="disabled"):
        run(gateway, ranking_prompt())
    assert not litellm.chat_bodies
    row = logs(gateway)[-1]
    assert row.error_type == "CONFIGURATION"


def test_the_worker_context_budget_is_enforced_before_sending(gateway, litellm):
    gateway.worker("RANKER", context_window=512)
    gateway.apply()
    prompt = Prompt("ranking", "v4", "FIT_ANALYSIS", "Score the job.", "FIT_ANALYSIS\n" + "word " * 2000)
    with pytest.raises(LLMContextOverflowError):
        run(gateway, prompt)
    assert not litellm.chat_bodies and logs(gateway)[-1].error_type == "CONTEXT_OVERFLOW"


def test_max_concurrency_bounds_requests_in_flight():
    worker = ResolvedWorker(worker_type="RANKER", mode="TIER", configured="X", model="m", max_concurrency=1)
    held = threading.Event()
    release = threading.Event()

    def hold():
        with concurrency_slot(worker, 99, 1):
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(2)
    with concurrency_slot(worker, 99, 0.05) as free:
        assert free is False
    release.set()
    thread.join()
    with concurrency_slot(worker, 99, 0.05) as free:
        assert free is True


def test_worker_test_runs_a_fixture_with_the_draft(gateway, litellm):
    litellm.answer("tier-2-reasoning", {"overall_fit_score": 80, "confidence": 0.9, "reasoning_summary": "ok"})
    out = gateway.call("POST", "/workers/RANKER/test", json={"configuration": "draft"})
    assert out["success"] and out["schema_ok"] and out["answered_by"] == "ollama_chat/gpt-oss:20b"
    assert out["routing"]["model_group"] == "tier-2-reasoning" and out["input_tokens"] == 40
    sent = litellm.chat_bodies[-1]["messages"][1]["content"]
    assert "Sample Candidate" in sent and "MODEL_TEST" in sent  # fictional fixture data only
    litellm.answer("tier-2-reasoning", {"overall_fit_score": "high"})
    failed = gateway.call("POST", "/workers/RANKER/test", json={})
    assert failed["success"] is False and failed["schema_ok"] is False


def test_model_test_is_tiny_and_records_health(gateway, litellm):
    litellm.answer("tier-1-fast", {"ok": True})
    out = gateway.call("POST", "/models/tier-1-fast/test")
    assert out["success"] and out["structured_output"] is True and out["model_group"] == "tier-1-fast"
    body = litellm.chat_bodies[-1]
    assert body["max_tokens"] == 20 and len(body["messages"][1]["content"]) < 100
    litellm.unavailable.add("tier-3-review")
    failed = gateway.call("POST", "/models/tier-3-review/test")
    assert failed["success"] is False and "unavailable" in failed["error"]
    health = {m["name"]: m["health_status"] for m in gateway.get("/models")["models"]}
    assert health["tier-1-fast"] == "HEALTHY" and health["tier-3-review"] == "UNHEALTHY"
