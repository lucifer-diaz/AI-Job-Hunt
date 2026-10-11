"""Model failover: route health, thresholds, ordering by algorithm, the API and an end-to-end switch."""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.main import app
from app.models import AppSetting, FailoverEvent, LLMRequestLog, ModelRouteState
from app.services import failover, failover_settings, model_health, model_resolution
from app.services.llm import LLMClient, LLMRateLimitError
from app.services.llm_telemetry import LLMCall
from app.services.model_resolution import resolve
from tests.conftest import ensure_test_user
from tests.test_ai_models import snapshot
from tests.test_litellm_gateway import gateway, ranking_prompt, run  # noqa: F401

client = TestClient(app)
CFG = failover_settings.resolve({})


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        with SessionLocal() as db:
            db.execute(delete(ModelRouteState))
            db.execute(delete(FailoverEvent))
            db.execute(delete(LLMRequestLog).where(LLMRequestLog.request_id.like("failover-test-%")))
            for row in db.scalars(select(AppSetting)).all():  # thresholds a test saved never leak into the next one
                if "failover" in (row.preferences or {}):
                    row.preferences = {k: v for k, v in row.preferences.items() if k != "failover"}
            db.commit()
        model_health.reset_cache()
        failover.reset_cache()
    wipe()
    yield
    wipe()


def outcome(owner, model="mid", status="SUCCEEDED", error_type=None, worker="RANKER", fallback_used=False):
    model_health.record_outcome({"user_id": owner, "provider": "litellm", "requested_model": model,
                                 "status": status, "error_type": error_type, "worker_type": worker,
                                 "fallback_used": fallback_used})


def route(owner, model="mid", worker=""):
    with SessionLocal() as db:
        return db.scalar(select(ModelRouteState).where(ModelRouteState.user_id == owner,
                                                       ModelRouteState.model == model,
                                                       ModelRouteState.worker_type == worker))


def events(owner):
    with SessionLocal() as db:
        return db.scalars(select(FailoverEvent).where(FailoverEvent.user_id == owner).order_by(FailoverEvent.id)).all()


# ───────────── judging a window: pure ─────────────

def rows(n_ok=0, n_fail=0, kind="PROVIDER_ERROR", latency=100):
    return ([{"status": "SUCCEEDED", "latency_ms": latency}] * n_ok
            + [{"status": "FAILED", "error_type": kind}] * n_fail)


def test_too_few_requests_are_never_judged():
    assert model_health.judge(model_health.summarize(rows(0, 4)), CFG) is None  # 4 < min_samples (5)


def test_failure_rate_rate_limits_schema_and_latency_breach_thresholds():
    assert model_health.judge(model_health.summarize(rows(2, 6)), CFG)[0] == "ERROR_RATE"
    assert model_health.judge(model_health.summarize(rows(10, 3, "RATE_LIMIT")), CFG)[0] == "RATE_LIMIT"
    assert model_health.judge(model_health.summarize(rows(2, 6, "SCHEMA_VALIDATION")), CFG)[0] == "SCHEMA"
    slow = model_health.summarize(rows(10, 0, latency=9000))
    assert model_health.judge(slow, CFG) is None  # no latency limit by default
    assert model_health.judge(slow, {**CFG, "max_p95_ms": 5000})[0] == "LATENCY"
    assert model_health.judge(slow, {**CFG, "degradation_pct": 50}, baseline_p95=2000)[0] == "LATENCY"
    assert model_health.judge(slow, {**CFG, "degradation_pct": 50}, baseline_p95=8000) is None


def test_configuration_errors_and_failovers_are_counted_correctly():
    stats = model_health.summarize(rows(5, 3, "CONFIGURATION") + [{"status": "SUCCEEDED", "fallback_used": True}])
    assert stats.samples == 6 and stats.failures == 1  # config errors say nothing about a model; a failover counts


# ───────────── per-request outcomes rest and recover a route ─────────────

def test_a_rate_limit_rests_the_model_once_and_notifies(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")  # still cooling: the same outage, not a new event
    state = route(owner)
    assert state.state == "COOLING" and state.cooling_until > model_health.utcnow()
    assert model_health.resting(owner, "mid", "RANKER") and not model_health.resting(owner, "other", "RANKER")
    found = events(owner)
    assert len(found) == 1 and found[0].cause == "RATE_LIMIT" and "mid" in found[0].message


def test_failures_in_a_row_rest_the_route_for_that_worker_only(owner):
    for _ in range(CFG["consecutive_failures"] - 1):
        outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    assert not model_health.resting(owner, "mid", "RANKER")
    outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    assert model_health.resting(owner, "mid", "RANKER") and not model_health.resting(owner, "mid", "TAILOR")


def test_a_success_between_failures_resets_the_run(owner):
    for _ in range(CFG["consecutive_failures"] - 1):
        outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    outcome(owner)  # success
    outcome(owner, status="FAILED", error_type="PROVIDER_ERROR")
    assert not model_health.resting(owner, "mid", "RANKER")


def test_disabled_failover_and_configuration_errors_never_rest_a_route(owner):
    outcome(owner, status="FAILED", error_type="CONFIGURATION")
    assert route(owner) is None
    with SessionLocal() as db:
        failover_settings.update(db, owner, {"enabled": False})
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    assert route(owner) is None
    with SessionLocal() as db:
        failover_settings.update(db, owner, {"enabled": True})


def test_a_rested_route_recovers_after_its_cooldown_and_enough_probe_successes(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    with SessionLocal() as db:
        db.query(ModelRouteState).update({"cooling_until": model_health.utcnow() - timedelta(seconds=1)})
        db.commit()
    model_health.reset_cache()
    assert not model_health.resting(owner, "mid", "RANKER")  # half-open: traffic probes it
    outcome(owner)
    assert route(owner).state == "COOLING"  # one success is not enough (recovery_successes = 2)
    outcome(owner)
    assert route(owner).state == "HEALTHY"
    assert [e.cause for e in events(owner)] == ["RATE_LIMIT", "RECOVERED"]


def test_a_failed_probe_rests_the_route_again_for_longer(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    first = route(owner).cooling_until - model_health.utcnow()
    with SessionLocal() as db:
        db.query(ModelRouteState).update({"cooling_until": model_health.utcnow() - timedelta(seconds=1)})
        db.commit()
    model_health.reset_cache()
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    state = route(owner)
    assert state.stats["trips"] == 2 and state.cooling_until - model_health.utcnow() > first


def test_the_sweep_rests_a_route_whose_window_breaches_a_threshold(owner):
    with SessionLocal() as db:
        for i in range(8):
            ok = i < 2
            db.add(LLMRequestLog(request_id=f"failover-test-{uuid.uuid4()}", user_id=owner, provider="litellm", stage="OTHER",
                                 requested_model="mid", model="mid", worker_type="RANKER",
                                 status="SUCCEEDED" if ok else "FAILED", error_type=None if ok else "PROVIDER_ERROR",
                                 latency_ms=100, created_at=model_health.utcnow()))
        db.commit()
        result = model_health.evaluate(db, owner)
    assert result["tripped"] == 1 and route(owner, worker="RANKER").state == "DEGRADED"
    assert events(owner)[0].cause == "ERROR_RATE"
    with SessionLocal() as db:
        assert model_health.evaluate(db, owner)["tripped"] == 0  # already resting: no second event
    assert len(events(owner)) == 1


# ───────────── choosing a model ─────────────

def worker():
    return resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast",
                                    "secondary_models": ["deep", "mid", "fast"]}), "RANKER")


def test_secondary_models_are_ordered_and_deduplicated():
    w = worker()
    assert w.model == "mid" and w.chain() == ["fast", "deep"]


def test_a_healthy_primary_is_left_alone(owner):
    w = worker()
    assert failover.apply(w, owner) is w


def test_a_resting_primary_moves_to_the_next_healthy_model_and_keeps_the_rest_as_fallbacks(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    moved = failover.apply(worker(), owner)
    assert moved.model == "fast" and moved.failed_over_from == "mid"
    assert moved.chain() == ["deep", "mid"]  # the resting primary stays last, a last resort
    assert "failover" in moved.note


def test_priority_prefers_configured_order_and_other_algorithms_prefer_the_better_route(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    with SessionLocal() as db:
        db.add_all([ModelRouteState(user_id=owner, worker_type="RANKER", model="fast", score=60.0,
                                    stats={"p95_ms": 900, "failure_pct": 20}, updated_at=model_health.utcnow()),
                    ModelRouteState(user_id=owner, worker_type="RANKER", model="deep", score=95.0,
                                    stats={"p95_ms": 300, "failure_pct": 1}, updated_at=model_health.utcnow())])
        db.commit()
    model_health.reset_cache()
    for algorithm, expected in (("PRIORITY", "fast"), ("LATENCY", "deep"), ("LEAST_ERRORS", "deep"),
                                ("WEIGHTED", "deep")):
        with SessionLocal() as db:
            failover_settings.update(db, owner, {"algorithm": algorithm})
        failover.reset_cache()
        assert failover.apply(worker(), owner).model == expected, algorithm


def test_a_worker_without_secondary_models_is_never_changed(owner):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    alone = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid"}), "RANKER")
    assert failover.apply(alone, owner) is alone


def test_failover_fails_open(owner, monkeypatch):
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    monkeypatch.setattr(failover, "order", lambda *a, **k: 1 / 0)
    w = worker()
    assert failover.apply(w, owner) is w


# ───────────── thresholds ─────────────

def test_settings_are_validated_and_defaults_are_not_stored(owner):
    with SessionLocal() as db:
        out = failover_settings.update(db, owner, {"max_failure_pct": 30, "algorithm": "LATENCY"})
        assert out["values"]["max_failure_pct"] == 30 and out["values"]["algorithm"] == "LATENCY"
        for bad in ({"max_failure_pct": 0}, {"algorithm": "RANDOM"}, {"nonsense": 1}, {"enabled": "yes"},
                    {"cooldown_seconds": 3000, "max_cooldown_seconds": 100}):
            with pytest.raises(failover_settings.FailoverSettingError):
                failover_settings.update(db, owner, bad)
        failover_settings.update(db, owner, {"max_failure_pct": 50, "algorithm": "PRIORITY"})
        assert failover_settings.for_user(db, owner) == failover_settings.resolve({})


def test_api_settings_routes_events_and_reset(owner):
    assert client.get("/api/failover/settings").json()["values"]["algorithm"] == "PRIORITY"
    assert client.put("/api/failover/settings", json={"values": {"max_rate_limits": 2}}).json()["values"][
        "max_rate_limits"] == 2
    assert client.put("/api/failover/settings", json={"values": {"max_rate_limits": 0}}).status_code == 400
    client.put("/api/failover/settings", json={"values": {"max_rate_limits": 3}})
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    listed = client.get("/api/failover/routes").json()
    assert listed["resting"] == 1 and listed["routes"][0]["model"] == "mid" and listed["routes"][0]["resting"]
    feed = client.get("/api/failover/events?since_id=0").json()
    assert len(feed["events"]) == 1 and feed["latest_id"] == feed["events"][0]["id"] and feed["notify"]
    assert client.get(f"/api/failover/events?since_id={feed['latest_id']}").json()["events"] == []
    client.post("/api/failover/events/ack", json={"up_to_id": feed["latest_id"]})
    assert client.get("/api/failover/events?since_id=0").json()["events"][0]["acknowledged"]
    assert client.post("/api/failover/routes/reset", json={"model": "mid"}).status_code == 200
    assert client.get("/api/failover/routes").json()["resting"] == 0
    assert client.post("/api/failover/routes/reset", json={"model": "never-seen"}).status_code == 404


# ───────────── end to end through the gateway ─────────────

def test_a_rate_limited_primary_fails_over_to_a_secondary_model_and_back(gateway, litellm):  # noqa: F811
    gateway.worker("RANKER", fallback_model="tier-1-fast", secondary_models=["tier-3-review"])
    gateway.apply()
    uid = gateway.user_id
    primary = model_resolution.get(uid, "RANKER").model
    assert model_resolution.get(uid, "RANKER").chain() == ["tier-1-fast", "tier-3-review"]

    litellm.rate_limited.add(primary)
    with pytest.raises(LLMRateLimitError):
        run(gateway, ranking_prompt())  # the 429 itself still fails this one request ...
    assert model_health.resting(uid, primary, "RANKER")

    result = run(gateway, ranking_prompt())  # ... and the next one goes to the secondary model
    assert result.routing["requested_model"] == "tier-1-fast"
    with SessionLocal() as db:
        last = db.scalars(select(LLMRequestLog).where(LLMRequestLog.user_id == uid)
                          .order_by(LLMRequestLog.id.desc())).first()
    assert last.requested_model == "tier-1-fast" and last.routing_cause == "FAILOVER"
    assert last.failed_over_from == primary  # the request's own fact, read by Running workers for finished tasks
    assert [e.cause for e in events(uid)] == ["RATE_LIMIT"]
    with SessionLocal() as db:  # Running workers reads the same plan: where the next request goes and why
        plan = LLMClient.for_user(db, uid).route_for("RANKER")
    assert plan["route"] == "LITELLM" and plan["model"] == "tier-1-fast" and plan["failed_over_from"] == primary
    assert plan["primary"] == primary and plan["mode"] == "TIER" and plan["configured"] == "PRIMARY_REASONING"
    assert LLMClient and LLMCall  # imported for the fixtures' sake


# ───────────── a model's own fallbacks ─────────────

def test_a_models_own_fallbacks_follow_the_workers_backups_and_take_over_when_it_rests(owner):
    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast"})
    snap["models"]["mid"]["fallback_models"] = ["deep", "fast", "mid", "off"]  # repeats, itself, a disabled one
    w = resolve(snap, "RANKER")
    assert w.model_fallbacks == ("deep", "fast") and w.chain() == ["fast", "deep"]
    alone = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid"}) | {
        "models": {**snapshot()["models"], "mid": {"enabled": True, "fallback_models": ["deep"]}}}, "RANKER")
    assert alone.chain() == ["deep"]  # no backups on the worker: the model's fallback still applies
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    moved = failover.apply(alone, owner)
    assert moved.model == "deep" and moved.failed_over_from == "mid" and moved.chain() == ["mid"]


def test_a_model_rests_for_its_own_time_before_it_is_tried_again(owner, monkeypatch):
    monkeypatch.setattr(model_resolution, "active_snapshot",
                        lambda uid: (3, {"models": {"mid": {"enabled": True, "rest_seconds": 3600}}}))
    outcome(owner, status="FAILED", error_type="RATE_LIMIT")
    rest = route(owner).cooling_until - model_health.utcnow()
    assert timedelta(minutes=59) < rest <= timedelta(hours=1)
    assert model_health._cooldown(CFG, 1) == timedelta(seconds=CFG["cooldown_seconds"])  # no rest time: the setting
    assert model_health._cooldown(CFG, 3, 3600) == timedelta(hours=1)  # a model's own rest time never grows
    assert model_health._cooldown(CFG, 2) == timedelta(seconds=2 * CFG["cooldown_seconds"])  # the setting doubles


def test_a_rate_limited_model_test_rests_that_model_not_the_gateways_admin_calls():
    from app.services.external_api import cooldown_provider

    # a model test (service model_test) is a model request like a worker's: a 429 rests only that model, so Apply
    # and Sync (admin calls, no model) still reach LiteLLM
    assert cooldown_provider("LITELLM", "model_test", {"model": "groq/llama-guard-3-8b"}) == "LITELLM:groq/llama-guard-3-8b"
    assert cooldown_provider("LITELLM", "llm", {"model": "mid"}) == "LITELLM:mid"
    assert cooldown_provider("LITELLM", "admin", None) == "LITELLM"
    assert cooldown_provider("APIFY", "actor", {"model": "x"}) == "APIFY"


def test_apply_during_a_litellm_cooldown_is_a_429_not_a_server_error(monkeypatch):
    from app.services import ai_model_config
    from app.services.external_api import RateLimitedCooldown

    def cooling(*_a, **_k):
        raise RateLimitedCooldown("LITELLM", 12)

    monkeypatch.setattr(ai_model_config, "apply", cooling)
    response = client.post("/api/settings/ai/apply", json={})
    assert response.status_code == 429 and "LITELLM" in response.json()["detail"]


def test_a_resting_backup_moves_behind_the_healthy_ones_while_the_primary_keeps_its_place(owner):
    w = resolve(snapshot(RANKER={"mode": "FIXED", "primary_model": "mid", "fallback_model": "fast",
                                 "secondary_models": ["deep"]}), "RANKER")
    assert w.chain() == ["fast", "deep"]
    outcome(owner, model="fast", status="FAILED", error_type="RATE_LIMIT")
    moved = failover.apply(w, owner)
    assert moved.model == "mid" and moved.failed_over_from is None and moved.chain() == ["deep", "fast"]
