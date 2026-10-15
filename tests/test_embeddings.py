"""EmbeddingService: configured (never hard-coded), served through LiteLLM's instrumented client, size-checked, and
recorded with the cost LiteLLM reported (NULL when it reported none) and never the text."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ExternalAPIRequestLog
from app.services import model_resolution
from app.services.embeddings import EmbeddingError, EmbeddingService, EmbeddingUnavailable
from app.settings import get_settings
from tests.conftest import make_user
from tests.litellm_fake import embedding_deployment

SECRET_TEXT = "Reduced incidents by 45% at Acme — confidential phrasing 7f3a"


def service(user_id: int) -> EmbeddingService:
    with SessionLocal() as db:
        db.info["user_id"] = user_id
        return EmbeddingService.for_user(db, user_id)


def configure(acct, litellm, name="embed-small", size=16, *, apply=True):
    litellm.deployments.append(embedding_deployment(name, "openai/text-embedding-3-small", size))
    acct.gateway(True)
    acct.call("POST", "/models/sync")
    acct.worker("EMBEDDER", model_mode="FIXED", primary_model=name, enabled=True)
    if apply:
        acct.apply()
    model_resolution.invalidate()


def rows(user_id: int) -> list[ExternalAPIRequestLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(ExternalAPIRequestLog).where(ExternalAPIRequestLog.user_id == user_id,
                                                                   ExternalAPIRequestLog.service == "embedding")))


def test_mock_provider_embeds_locally_with_the_configured_test_size():
    user_id = make_user(SessionLocal)
    svc = service(user_id)
    assert (svc.identity.provider, svc.identity.dimensions) == ("mock", get_settings().rag_mock_dimensions)
    vectors = svc.embed_documents(["Kafka pipelines", "Go services"]).vectors
    assert len(vectors) == 2 and all(len(v) == svc.identity.dimensions for v in vectors)
    assert svc.embed_query("Kafka pipelines") == vectors[0]


def test_a_direct_provider_has_no_embeddings(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    user_id = make_user(SessionLocal)
    with pytest.raises(EmbeddingUnavailable) as caught:
        service(user_id)
    assert caught.value.code == "UNSUPPORTED_PROVIDER"


def test_the_embedder_is_seeded_off_and_never_borrows_a_chat_tier(account):
    account.gateway(True)
    worker = next(w for w in account.get("")["workers"] if w["worker_type"] == "EMBEDDER")
    assert worker["capability"] == "EMBEDDING" and worker["config"]["enabled"] is False
    assert worker["resolved"]["model"] is None
    model_resolution.invalidate()
    with pytest.raises(EmbeddingUnavailable) as caught:
        service(account.user_id)
    assert caught.value.code == "NOT_CONFIGURED"
    assert account.call("POST", "/preview")["can_apply"] is True  # an unconfigured embedder blocks nothing


def test_discovery_learns_an_embedding_model_and_its_vector_size(account, litellm):
    litellm.deployments.append(embedding_deployment("embed-small", "openai/text-embedding-3-small", 16))
    account.gateway(True)
    account.call("POST", "/models/sync")
    model = next(m for m in account.get("")["models"] if m["name"] == "embed-small")
    assert (model["model_type"], model["embedding_dimensions"]) == ("EMBEDDING", 16)


def test_validation_keeps_embedding_and_chat_models_apart(account, litellm):
    configure(account, litellm, apply=False)
    account.worker("RANKER", model_mode="FIXED", primary_model="embed-small")
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any(e["path"].startswith("workers.RANKER") and "embedding model" in e["message"] for e in errors)
    account.worker("RANKER", model_mode="TIER", tier_name="PRIMARY_REASONING")
    account.worker("EMBEDDER", model_mode="FIXED", primary_model="tier-1-fast", enabled=True)
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any(e["path"] == "workers.EMBEDDER.primary_model" and "not an embedding model" in e["message"]
               for e in errors)


def test_no_chat_failover_slot_may_hold_an_embedding_model(account, litellm):
    litellm.deployments.append(embedding_deployment("embed-other", "openai/other", 16))
    configure(account, litellm, apply=False)
    # A chat primary with an embedding model as its fallback or a secondary: failover would send it a chat request.
    account.worker("RANKER", model_mode="FIXED", primary_model="tier-1-fast", fallback_model="embed-small",
                   secondary_models=["embed-other"])
    errors = account.call("POST", "/preview")["validation"]["errors"]
    paths = {e["path"] for e in errors if "embedding model" in e["message"]}
    assert {"workers.RANKER.fallback_model", "workers.RANKER.secondary_models.0"} <= paths
    account.worker("RANKER", fallback_model=None, secondary_models=[])
    account.call("PUT", "/models/tier-1-fast", json={"revision": account.revision(), "fallback_models": ["embed-small"]})
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any(e["path"] == "models.tier-1-fast.fallback_models.0" and "embedding model" in e["message"]
               for e in errors)


def test_an_embedding_model_of_unknown_size_cannot_be_applied_until_a_test_measures_it(account, litellm):
    configure(account, litellm, name="embed-nosize", size=None, apply=False)
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any("vector size" in e["message"] for e in errors)
    tested = account.call("POST", "/models/embed-nosize/test")
    assert tested["success"] and tested["embedding_dimensions"] == 8  # the fake's default size
    assert account.call("POST", "/preview")["can_apply"] is True


def test_an_embedder_has_no_fallbacks(account, litellm):
    litellm.deployments.append(embedding_deployment("embed-other", "openai/other", 16))
    configure(account, litellm, apply=False)
    account.worker("EMBEDDER", fallback_model="embed-other")
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any(e["path"] == "workers.EMBEDDER.fallback_model" for e in errors)


def test_embeddings_go_through_litellm_with_reported_cost_and_no_text_in_telemetry(account, litellm):
    configure(account, litellm)
    svc = service(account.user_id)
    assert (svc.identity.provider, svc.identity.model, svc.identity.dimensions) == ("litellm", "embed-small", 16)
    result = svc.embed_documents([SECRET_TEXT, "Go services"])
    assert len(result.vectors) == 2 and all(len(v) == 16 for v in result.vectors)
    assert result.answered_model == "openai/text-embedding-3-small" and result.input_tokens == 14
    assert litellm.embed_bodies[-1]["model"] == "embed-small"
    [row] = rows(account.user_id)
    assert (row.provider, row.operation, row.status) == ("LITELLM", "embed_documents", "SUCCEEDED")
    assert float(row.estimated_cost) == pytest.approx(0.00002) and row.cost_source == "provider_reported"
    meta = row.request_metadata or {}
    assert meta["model"] == "embed-small" and meta["answered_model"] == "openai/text-embedding-3-small"
    stored = json.dumps({"meta": row.request_metadata, "payload": row.payload, "error": row.error_message})
    assert "confidential phrasing" not in stored


@pytest.mark.parametrize("switch", ["account", "server"])
def test_payload_capture_never_keeps_embedding_text_or_vectors(account, litellm, monkeypatch, switch):
    from app.services import llm_telemetry

    if switch == "account":  # the account's own capture switch (Monitoring → Logs)
        monkeypatch.setattr(llm_telemetry, "account_captures", lambda user_id: True)
    else:  # EXTERNAL_API_DEBUG_PAYLOADS, for every account
        monkeypatch.setattr(get_settings(), "external_api_debug_payloads", True)
    configure(account, litellm)
    service(account.user_id).embed_documents([SECRET_TEXT])
    [row] = rows(account.user_id)
    assert row.status == "SUCCEEDED" and row.payload is None and (row.request_size_bytes or 0) > 0
    meta = row.request_metadata or {}
    assert "request_payload" not in meta and "response_payload" not in meta
    assert "confidential phrasing" not in json.dumps({"meta": meta, "payload": row.payload}, default=str)


def test_unreported_cost_stays_unknown(account, litellm):
    configure(account, litellm)
    litellm.embed_cost = None
    service(account.user_id).embed_query("Kafka")
    [row] = rows(account.user_id)
    assert row.estimated_cost is None and row.cost_source == "unknown"


def test_a_vector_of_the_wrong_size_is_refused(account, litellm):
    configure(account, litellm)
    litellm.embed_size_override = 8
    with pytest.raises(EmbeddingError) as caught:
        service(account.user_id).embed_documents(["Kafka"])
    assert caught.value.code == "DIMENSION_MISMATCH"


def test_a_rate_limit_rests_only_the_embedding_model(account, litellm):
    configure(account, litellm)
    litellm.rate_limited.add("embed-small")
    svc = service(account.user_id)
    with pytest.raises(EmbeddingError) as caught:
        svc.embed_documents(["Kafka"])
    assert caught.value.code == "RATE_LIMITED"
    with pytest.raises(EmbeddingError) as again:  # cooling down: not even sent
        svc.embed_documents(["Kafka"])
    assert again.value.code == "RATE_LIMITED"
    assert len(litellm.embed_bodies) == 1
    from app.services.ai_model_config import gateway_settings
    from app.services.litellm_client import LiteLLMAPI

    with SessionLocal() as db:
        url, key = gateway_settings(db, account.user_id)
    with LiteLLMAPI(url, key, user_id=account.user_id, service="llm") as api:  # a chat model is not blocked
        assert api.chat({"model": "tier-1-fast", "messages": []}, metadata={"model": "tier-1-fast"}).body["choices"]


def test_the_embedder_worker_test_checks_the_size_against_the_configuration(account, litellm):
    configure(account, litellm)
    out = account.call("POST", "/workers/EMBEDDER/test", json={"configuration": "draft"})
    assert out["success"] and out["embedding_dimensions"] == 16
    litellm.embed_size_override = 8
    out = account.call("POST", "/workers/EMBEDDER/test", json={"configuration": "draft"})
    assert not out["success"] and "8-dimension" in out["error"]
