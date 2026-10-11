"""Model performance: per-request facts recorded by the LLM layer, and the aggregates computed from the raw rows."""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import LLMRequestLog
from app.services import model_performance as mp
from app.services.llm import LLMCall
from tests.conftest import make_user
from tests.litellm_fake import account, litellm  # noqa: F401  fixtures
from tests.test_litellm_gateway import gateway, logs, ranking_prompt, run  # noqa: F401  fixtures and helpers


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def row(user_id, **fields):
    values = dict(request_id=str(uuid.uuid4()), user_id=user_id, stage="RANKER", provider="litellm", model="qwen-32b",
                  requested_model="qwen-32b", worker_type="RANKER", prompt_name="ranking", prompt_version="v4",
                  input_tokens=1000, output_tokens=200, total_tokens=1200, usage_source="provider_reported",
                  estimated_context_tokens=1000, context_window_size=10_000, context_utilization_percent=10.0,
                  latency_ms=2000, generation_ms=1800, estimated_cost=Decimal("0.01"), cost_source="provider_reported",
                  status="SUCCEEDED", retry_count=0, structured_output_valid=True, structured_output_retry_count=0,
                  fallback_used=False, escalated=False, config_version=3,
                  created_at=utcnow() - timedelta(minutes=30), completed_at=utcnow())
    values.update(fields)
    return LLMRequestLog(**values)


@pytest.fixture
def user_id():
    return make_user(SessionLocal)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", False))
        return TestClient(app)
    return sign_in


def seed(user_id, rows):
    with SessionLocal() as db:
        db.add_all(rows)
        db.commit()


def fetch(user_id):
    with SessionLocal() as db:
        return db.execute(select(*mp.COLUMNS).where(LLMRequestLog.user_id == user_id)).all()


def test_gateway_request_records_generation_time_overhead_reasoning_tier_and_queue(gateway, litellm):  # noqa: F811
    run(gateway, ranking_prompt())
    entry = logs(gateway)[-1]
    assert entry.generation_ms == 860  # 900.5 − 40.2, both reported by LiteLLM
    assert entry.gateway_overhead_ms == 40
    assert entry.reasoning_tokens == 5 and entry.service_tier == "default"
    assert entry.ttft_ms is None  # nothing streams, so time to first token is unknown, not zero
    assert entry.queue_wait_ms is None  # no max-concurrency limit, so there is no queue to measure


def test_dimensions_are_computed_from_raw_rows(user_id):
    now = utcnow()
    seed(user_id, [
        row(user_id, workflow_type="job", workflow_entity_id=1, latency_ms=1000, generation_ms=500, output_tokens=100,
            ttft_ms=200, created_at=now - timedelta(minutes=10)),
        row(user_id, workflow_type="job", workflow_entity_id=1, latency_ms=3000, status="FAILED", error_type="TIMEOUT",
            structured_output_valid=None, estimated_cost=Decimal("0.02"), retry_count=1,
            created_at=now - timedelta(minutes=9)),
        row(user_id, workflow_type="job", workflow_entity_id=2, latency_ms=2000, structured_output_valid=False,
            structured_output_retry_count=1, fallback_used=True, created_at=now - timedelta(minutes=8)),
        row(user_id, workflow_type="job", workflow_entity_id=3, status="FAILED", error_type="RATE_LIMIT",
            structured_output_valid=None, estimated_cost=None, cost_source="unknown", latency_ms=100,
            created_at=now - timedelta(minutes=7)),
    ])
    out = mp.compare(fetch(user_id), "model", now - timedelta(hours=1), now, now)
    g = out["groups"][0]
    assert g["key"] == "qwen-32b" and g["requests"] == 4
    rel, lat, thr, cost, qual = g["reliability"], g["latency"], g["throughput"], g["cost"], g["quality"]
    assert (rel["success_rate"], rel["timeout_rate"], rel["rate_limit_rate"]) == (0.5, 0.25, 0.25)
    assert rel["retry_adjusted_success_rate"] == 0.25  # only the first request succeeded cleanly
    assert rel["fallback_rate"] == 0.25 and rel["schema_pass_rate"] == 0.5 and rel["schema_checked"] == 2
    assert (lat["p50_ms"], lat["p75_ms"], lat["p95_ms"]) == (1000, 2000, 3000)
    assert lat["ttft_p50_ms"] == 200 and lat["ttft_samples"] == 1
    # failed requests have no rate; request 1: TTFT known → 100 tokens in 800 ms (125/s); request 3: generation_ms
    # 1800 → 200 tokens in 1.8 s (111/s); the median of the two (nearest rank) is 111
    assert lat["tokens_per_sec"] == pytest.approx(111.1, abs=0.1) and lat["tokens_per_sec_samples"] == 2
    assert thr["peak_requests_per_min"] >= 1 and thr["peak_concurrency"] >= 1
    assert thr["requests_per_min"] == pytest.approx(4 / 60, abs=0.001)  # one hour window
    # cost: 0.01 + 0.02 + 0.01 priced, one unpriced stays unknown
    assert cost["total"] == pytest.approx(0.04) and cost["unpriced_requests"] == 1
    assert cost["tasks"] == 3 and cost["successful_tasks"] == 2
    assert cost["per_successful_task"] == pytest.approx(0.02)  # 0.04 over 2 done tasks, failed attempts included
    assert cost["per_1k_tasks"] == pytest.approx(1000 * 0.04 / 3, rel=1e-3)
    assert qual["retry_after_invalid_output_rate"] == 0.25
    assert out["totals"]["requests"] == 4 and g["share"]["requests"] == 1.0


def test_unknown_stays_unknown(user_id):
    seed(user_id, [row(user_id, generation_ms=None, ttft_ms=None, estimated_cost=None, cost_source="unknown",
                       output_tokens=None, input_tokens=None, usage_source="none", structured_output_valid=None,
                       fallback_used=None, context_window_size=None, context_utilization_percent=None)])
    b = mp.block(fetch(user_id), 3600)
    assert b["latency"]["tokens_per_sec"] is None and b["latency"]["ttft_p50_ms"] is None
    assert b["cost"]["total"] is None and b["cost"]["per_request"] is None and b["cost"]["per_successful_task"] is None
    assert b["reliability"]["schema_pass_rate"] is None and b["reliability"]["fallback_rate"] is None
    assert b["context"]["avg_utilization_percent"] is None and b["tokens"]["reasoning"] is None


def test_free_models_cost_zero_and_peak_concurrency_overlaps(user_id):
    now = utcnow()
    seed(user_id, [row(user_id, estimated_cost=Decimal(0), cost_source="free", latency_ms=60_000,
                       created_at=now - timedelta(minutes=5, seconds=i)) for i in range(3)])
    b = mp.block(fetch(user_id), 3600)
    assert b["cost"]["total"] == 0 and b["cost"]["free"] and b["throughput"]["peak_concurrency"] == 3


def test_every_grouping_works_and_shares_add_up(user_id):
    now = utcnow()
    seed(user_id, [row(user_id, model="a", config_version=1, routing_tier="SIMPLE", prompt_version="v3"),
                   row(user_id, model="b", config_version=2, routing_tier=None, escalated=True,
                       escalation_reason="LOW_CONFIDENCE", fallback_used=True, provider="ollama", worker_type=None)])
    items = fetch(user_id)
    for group_by in mp.GROUPINGS:
        out = mp.compare(items, group_by, now - timedelta(days=1), now, now)
        assert sum(g["requests"] for g in out["groups"]) == 2
    out = mp.compare(items, "model", now - timedelta(days=1), now, now)
    shares = {g["key"]: g["share"] for g in out["groups"]}
    assert shares["b"]["fallbacks"] == 1.0 and shares["b"]["escalations"] == 1.0 and shares["a"]["requests"] == 0.5
    keys = {g["key"] for g in mp.compare(items, "config_version", now - timedelta(days=1), now, now)["groups"]}
    assert keys == {"v1", "v2"}
    with pytest.raises(ValueError):
        mp.compare(items, "nonsense", now, now)


def test_api_groups_filters_and_isolates_accounts(user_id, signed_in):
    other = make_user(SessionLocal)
    seed(user_id, [row(user_id), row(user_id, model="gpt-oss", provider="ollama", config_version=4)])
    seed(other, [row(other, model="someone-elses")])
    client = signed_in(user_id)
    out = client.get("/api/monitoring/performance?group_by=model").json()
    assert {g["key"] for g in out["groups"]} == {"qwen-32b", "gpt-oss"}
    assert "unsupported" in out and out["outcomes"]["reviewer"]["available"] is False
    out = client.get("/api/monitoring/performance?group_by=provider&config_version=4").json()
    assert [g["key"] for g in out["groups"]] == ["ollama"]
    assert client.get("/api/monitoring/performance?group_by=day").json()["groups"][0]["requests"] == 2
    assert client.get("/api/monitoring/performance?group_by=bogus").status_code == 422
    assert "SECRET" not in str(out)


def test_orchestration_node_shows_model_health_and_detail(user_id, signed_in):
    seed(user_id, [row(user_id) for _ in range(3)] + [row(user_id, status="FAILED", error_type="TIMEOUT")])
    client = signed_in(user_id)
    node = client.get("/api/orchestration/graph").json()["nodes"]["ranker"]
    health = node["model_health"][0]
    assert health["model"] == "qwen-32b" and health["requests"] == 4 and health["success_rate"] == 0.75
    assert health["p95_latency_ms"] == 2000 and health["cost"] == pytest.approx(0.04)
    detail = client.get("/api/orchestration/nodes/ranker").json()["performance"]
    assert detail["models"]["groups"][0]["key"] == "qwen-32b" and detail["series"] and detail["config_versions"]
    assert client.get("/api/orchestration/nodes/discovery").json()["performance"] is None


def test_telemetry_failure_in_the_new_fields_never_breaks_the_worker(gateway, litellm):  # noqa: F811
    result = run(gateway, ranking_prompt())  # sanity: a normal call works
    assert result.data
    from app.services import llm
    from app.services.llm_telemetry import Attempt

    attempt = Attempt(provider="litellm", requested_model="m", user_id=None, call=LLMCall(), prompt_name=None,
                      prompt_version=None, system="", user="")
    llm._gateway_timings(attempt, {"overhead_duration_ms": "not a number", "response_duration_ms": object()})
    assert attempt.generation_ms is None  # garbage headers are ignored, not raised
