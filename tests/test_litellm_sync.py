"""LiteLLMConfigManager: what Job Hunt OS may change inside LiteLLM, and how it keeps LiteLLM whole when it fails."""
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AIConfigVersion, LLMRequestLog, WorkerModelConfig
from app.services import ai_advisor, model_resolution
from app.services.litellm_config_manager import FILE_HEADER, owner_tag
from app.services.litellm_routing_import import import_routing
from app.settings import get_settings
from tests.litellm_fake import MASTER_KEY, deployment

ROUTER = {"auto_router_enabled": True, "classifier": "HEURISTIC", "auto_router_model_name": "jobhunt-auto",
          "manage_auto_router": True, "tier_models": {"SIMPLE": "tier-1-fast", "MEDIUM": "tier-2-reasoning",
                                                      "COMPLEX": "tier-2-reasoning", "REASONING": "tier-3-review"}}


def add_local_model(account, name="local-small", **extra):
    return account.call("POST", "/models", expect=201, json={"revision": account.revision(), "litellm_model_name": name,
                                                             "ollama_model": "llama3.2:3b", "num_ctx": 4096,
                                                             "keep_alive": "5m", **extra})


def versions(account):
    with SessionLocal() as db:
        return db.scalar(select(func.count(AIConfigVersion.id)).where(AIConfigVersion.user_id == account.user_id))


@pytest.fixture
def synced(account):
    account.call("POST", "/models/sync")
    account.apply()
    return account


# ───────────── admin API mode ─────────────

def test_a_managed_local_model_and_the_auto_router_are_deployed_and_verified(synced, litellm):
    add_local_model(synced)
    synced.call("PUT", "/routing", json={"revision": synced.revision(), **ROUTER})
    preview = synced.call("POST", "/preview")
    assert {d["model_name"] for d in preview["sync_plan"]["create"]} == {"local-small", "jobhunt-auto"}
    out = synced.apply()
    assert out["status"] == "SYNCED" and out["sync"]["verified"] is True
    managed = {d["model_name"]: d for d in litellm.managed()}
    assert managed["local-small"]["litellm_params"] == {"model": "ollama_chat/llama3.2:3b",
                                                       "api_base": "http://localhost:11434", "num_ctx": 4096,
                                                       "keep_alive": "5m"}
    router = managed["jobhunt-auto"]["litellm_params"]
    assert router["model"] == "auto_router/complexity_router"
    assert router["complexity_router_config"] == {"classifier_type": "heuristic", "tiers": ROUTER["tier_models"]}
    info = managed["local-small"]["model_info"]
    assert info["jobhunt_managed"] is True and info["jobhunt_owner"] == owner_tag(synced.user_id)
    assert synced.call("POST", "/verify")["status"] == "SYNCED"


def test_changing_a_managed_model_replaces_its_deployment_and_removing_it_deletes_it(synced, litellm):
    add_local_model(synced)
    synced.apply()
    synced.call("PUT", "/models/local-small", json={"revision": synced.revision(), "num_ctx": 8192})
    synced.apply()
    assert [d["litellm_params"]["num_ctx"] for d in litellm.managed()] == [8192]
    synced.call("DELETE", f"/models/local-small?revision={synced.revision()}")
    out = synced.apply()
    assert [op["op"] for op in out["sync"]["operations"]] == ["delete"] and litellm.managed() == []


def test_a_failed_apply_restores_litellm_and_keeps_the_previous_version(synced, litellm):
    before = versions(synced)
    add_local_model(synced)
    synced.call("PUT", "/routing", json={"revision": synced.revision(), **ROUTER})
    litellm.fail_new_model_at = litellm.new_model_calls + 2  # the second create fails
    failed = synced.apply(expect=502)
    assert failed["detail"]["sync_result"]["rolled_back"] is True
    assert litellm.managed() == [], "the deployment created before the failure was removed again"
    assert versions(synced) == before
    data = synced.get("")
    assert data["connection"]["sync_status"] == "FAILED" and data["pending_changes"]
    assert model_resolution.get(synced.user_id, "INBOX_CLASSIFIER").model == "tier-1-fast"
    assert ("APPLY", "FAILED") in [(a["action"], a["result"]) for a in synced.get("/audit")["entries"]]
    litellm.fail_new_model_at = None
    assert synced.apply()["status"] == "SYNCED"  # the same draft applies once LiteLLM accepts it


def test_verification_failure_rolls_back(synced, litellm):
    add_local_model(synced)
    litellm.hide_new_models = True
    failed = synced.apply(expect=502)
    assert "did not report the new configuration" in failed["detail"]["sync_result"]["error"]
    assert versions(synced) == 2


def test_store_model_in_db_off_is_explained_and_nothing_changes(synced, litellm):
    litellm.store_model_in_db = False
    add_local_model(synced)
    failed = synced.apply(expect=502)
    assert "STORE_MODEL_IN_DB=True" in failed["detail"]["sync_result"]["error"]
    assert litellm.managed() == [] and versions(synced) == 2


def test_worker_only_changes_never_touch_litellm(synced, litellm):
    synced.worker("RANKER", temperature=0.1, fallback_model="tier-3-review")
    def writes():
        return [c for c in litellm.calls if c[0] != "GET" and c[1].startswith("/model/")]

    before = writes()
    out = synced.apply()
    assert out["sync"]["operations"] == [] and writes() == before


def test_unmanaged_litellm_configuration_is_never_touched_or_shadowed(synced, litellm):
    add_local_model(synced, name="tier-1-fast-copy")
    synced.apply()
    litellm.deployments.append(deployment("someone-elses", "ollama_chat/phi4:latest"))
    synced.call("PUT", "/routing", json={"revision": synced.revision(), **ROUTER,
                                         "auto_router_model_name": "tier-3-review"})
    errors = synced.call("POST", "/preview")["validation"]["errors"]
    assert any("tier-3-review that Job Hunt OS does not manage" in e["message"] for e in errors)
    synced.call("PUT", "/routing", json={"revision": synced.revision(), "auto_router_enabled": False})
    synced.call("DELETE", f"/models/tier-1-fast-copy?revision={synced.revision()}")
    synced.apply()
    assert {"tier-1-fast", "tier-2-reasoning", "tier-3-review", "cloud-mini", "someone-elses"} <= set(litellm.names())
    deletes = [c[2]["id"] for c in litellm.calls if c[1] == "/model/delete"]
    assert len(deletes) == 1  # only its own deployment


def test_another_accounts_managed_models_are_left_alone(synced, litellm):
    litellm.deployments.append(deployment("theirs", "ollama_chat/phi4:latest", info={
        "jobhunt_managed": True, "jobhunt_owner": "jobhunt:elsewhere:1", "jobhunt_fingerprint": "x"}))
    add_local_model(synced)
    synced.apply()
    assert "theirs" in litellm.names()


def test_literal_credentials_are_refused_and_references_kept(synced, litellm):
    response = synced.client.post("/api/settings/ai/models", json={
        "litellm_model_name": "cloud-own", "deployment_params": {"model": "openrouter/x/y", "api_key": "sk-literal-9999"}})
    assert response.status_code == 422 and "os.environ/NAME" in response.json()["detail"]["message"]
    synced.call("POST", "/models", expect=201, json={"litellm_model_name": "cloud-own", "deployment_params": {
        "model": "openrouter/x/y", "api_key": "os.environ/OPENROUTER_API_KEY"}})
    synced.apply()
    assert {d["model_name"]: d for d in litellm.managed()}["cloud-own"]["litellm_params"]["api_key"] == \
        "os.environ/OPENROUTER_API_KEY"
    everything = json.dumps([synced.get(""), synced.get("/versions"), synced.get("/audit")])
    assert "sk-literal-9999" not in everything and MASTER_KEY not in everything


def test_litellm_rejecting_the_router_config_blocks_the_apply(synced, litellm):
    litellm.reject_router = "tiers.REASONING names a model that does not exist"
    synced.call("PUT", "/routing", json={"revision": synced.revision(), **ROUTER})
    errors = synced.call("POST", "/preview")["validation"]["errors"]
    assert any("LiteLLM rejected the auto router config" in e["message"] for e in errors)


def test_llm_and_custom_classifiers_are_configured_as_litellm_expects(synced, litellm):
    synced.call("PUT", "/routing", json={"revision": synced.revision(), **ROUTER, "classifier": "LLM",
                                         "classifier_model": "tier-1-fast"})
    synced.apply()
    config = {d["model_name"]: d for d in litellm.managed()}["jobhunt-auto"]["litellm_params"]["complexity_router_config"]
    assert config["classifier_type"] == "llm" and config["classifier_llm_config"] == {"model": "tier-1-fast"}
    synced.call("PUT", "/routing", json={"revision": synced.revision(), "classifier": "CUSTOM"})
    assert any("classifier needs the dotted path" in e["message"]
               for e in synced.call("POST", "/preview")["validation"]["errors"])
    synced.call("PUT", "/routing", json={"revision": synced.revision(), "classifier_plugin": "jobhunt_classifier.router"})
    synced.apply()
    config = {d["model_name"]: d for d in litellm.managed()}["jobhunt-auto"]["litellm_params"]["complexity_router_config"]
    assert config["classifier_type"] == "custom" and config["classifier_plugin"] == "jobhunt_classifier.router"


def test_sync_disabled_refuses_changes_that_live_in_litellm(synced):
    synced.call("PUT", "/connection", json={"sync_mode": "disabled"})
    add_local_model(synced)
    errors = synced.call("POST", "/preview")["validation"]["errors"]
    assert any("synchronization is disabled" in e["message"] for e in errors)


def test_verify_detects_a_managed_deployment_removed_behind_its_back(synced, litellm):
    add_local_model(synced)
    synced.apply()
    litellm.deployments = [d for d in litellm.deployments if d["model_name"] != "local-small"]
    out = synced.call("POST", "/verify")
    assert out["status"] == "OUT_OF_SYNC" and out["differences"] == ["missing local-small"]


# ───────────── config file mode ─────────────

@pytest.fixture
def file_mode(synced, tmp_path, monkeypatch):
    managed, main = tmp_path / "jobhunt_models.yaml", tmp_path / "config.yaml"
    main.write_text("# the operator's config, with comments\nmodel_list: []\ninclude:\n  - jobhunt_models.yaml\n")
    settings = get_settings()
    monkeypatch.setattr(settings, "litellm_managed_config_path", str(managed))
    monkeypatch.setattr(settings, "litellm_config_path", str(main))
    synced.call("PUT", "/connection", json={"sync_mode": "config_file"})
    return synced, managed, main


def test_config_file_mode_writes_only_its_own_file_and_waits_for_a_restart(file_mode, litellm):
    account, managed, main = file_mode
    original = main.read_text()
    add_local_model(account)
    out = account.apply()
    assert out["status"] == "PENDING" and out["sync"]["restart_required"] is True
    text = managed.read_text()
    assert text.startswith(FILE_HEADER) and f"# owner: {owner_tag(account.user_id)}" in text
    assert "model_name: local-small" in text and "num_ctx: 4096" in text
    assert main.read_text() == original, "the operator's config is never edited"
    assert account.get("")["connection"]["sync_status"] == "PENDING"
    assert account.call("POST", "/verify")["status"] == "OUT_OF_SYNC"  # LiteLLM has not restarted yet


def test_config_file_mode_writes_nothing_while_nothing_is_managed(file_mode):
    account, managed, _ = file_mode
    account.worker("RANKER", temperature=0.3)
    out = account.apply()
    assert out["status"] == "SYNCED" and not managed.exists()


def test_config_file_mode_needs_the_include_and_its_own_file(file_mode):
    account, managed, main = file_mode
    main.write_text("model_list: []\n")
    add_local_model(account)
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any("does not include the managed file" in e["message"] for e in errors)
    main.write_text("include:\n  - jobhunt_models.yaml\n")
    managed.write_text("model_list:\n  - model_name: hand-written\n")
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any("was not written by Job Hunt OS" in e["message"] for e in errors)


def test_config_file_mode_restores_the_previous_file_when_the_restart_does_not_verify(file_mode, litellm, monkeypatch):
    account, managed, _ = file_mode
    add_local_model(account)
    account.apply()
    first = managed.read_text()
    monkeypatch.setattr(get_settings(), "litellm_restart_command", f"{sys.executable} -c pass")
    monkeypatch.setattr(get_settings(), "litellm_restart_wait_seconds", 1)
    account.call("PUT", "/models/local-small", json={"revision": account.revision(), "num_ctx": 8192})
    failed = account.apply(expect=502)  # the fake never loads files, so the new deployment never appears
    assert failed["detail"]["sync_result"]["rolled_back"] is True
    assert managed.read_text() == first and not Path(str(managed) + ".tmp").exists()


# ───────────── telemetry import, advisor, classifier plugin ─────────────

def test_routing_decisions_are_imported_from_litellm_spend_logs(synced, litellm):
    with SessionLocal() as db:
        rows = [LLMRequestLog(request_id=f"r-{i}-{synced.user_id}", user_id=synced.user_id, provider="litellm",
                              stage="INBOX", worker_type="INBOX_CLASSIFIER", model_mode="AUTO_ROUTER",
                              configured_model="AUTO_ROUTER", litellm_call_id=f"call-{i}-{synced.user_id}",
                              created_at=ai_advisor.utcnow() - timedelta(hours=2 if i else 0))
                for i in range(2)]
        db.add_all(rows)
        db.commit()
    litellm.spend[f"call-0-{synced.user_id}"] = {"request_id": "call-0", "metadata": {
        "routing_decision": {"tier": "MEDIUM", "cause": "heuristic", "routed_model": "tier-2-reasoning"}}}
    result = import_routing(SessionLocal, ai_advisor.utcnow())
    assert result["updated"] >= 1 and result["unavailable"] >= 1
    with SessionLocal() as db:
        found = {r.litellm_call_id: (r.routing_tier, r.routing_cause) for r in db.scalars(
            select(LLMRequestLog).where(LLMRequestLog.user_id == synced.user_id)).all()}
    assert found[f"call-0-{synced.user_id}"] == ("MEDIUM", "heuristic")
    assert found[f"call-1-{synced.user_id}"] == (None, "unavailable")  # an hour old and still no log: stop asking


def test_the_advisor_recommends_model_changes_but_never_makes_them(synced):
    routing = {"workers": [
        {"worker_type": "COMPANY_INSIGHTS", "requests": 50, "escalations": 34, "escalation_rate": 0.68,
         "failure_rate": 0, "schema_failure_rate": 0, "fallback_rate": 0, "inference_share": 0.1,
         "p95_latency_ms": 900, "configured": ["FAST"], "models": []},
        {"worker_type": "INBOX_CLASSIFIER", "requests": 100, "escalations": 8, "escalation_rate": 0.08,
         "failure_rate": 0, "schema_failure_rate": 0, "fallback_rate": 0, "inference_share": 0.05,
         "p95_latency_ms": 400, "configured": ["AUTO_ROUTER"], "models": []},
        {"worker_type": "REVIEWER", "requests": 40, "escalations": 0, "escalation_rate": None,
         "failure_rate": 0, "schema_failure_rate": 0, "fallback_rate": 0.2, "inference_share": 0.34,
         "p95_latency_ms": 60000, "configured": ["DEEP_REVIEW"], "models": []}],
        "worker_models": [{"worker_type": "RANKER", "model": "tier-1-fast", "requests": 20,
                           "schema_failure_rate": 0.25}]}
    findings = ai_advisor.detect_routing_findings(routing)
    text = " | ".join(f["finding"] + " " + f["recommendation"]["recommended_change"] for f in findings)
    assert "68.0% of Company Insights requests escalate from FAST" in text
    assert "92.0% of Inbox Classifier requests succeed" in text and "Keep the current Inbox Classifier" in text
    assert "Reviewer uses 34.0% of all inference time" in text and "quality-escalation" in text
    assert "answered by the fallback model" in text and "invalid structured output for Ranker 25.0%" in text
    assert {f["affected_stage"] for f in findings} <= set(ai_advisor.ADVISOR_STAGES)
    with SessionLocal() as db:
        before = [(w.worker_type, w.model_mode, w.primary_model, w.tier_name) for w in db.scalars(
            select(WorkerModelConfig).where(WorkerModelConfig.user_id == synced.user_id)
            .order_by(WorkerModelConfig.worker_type)).all()]
        ai_advisor.analyse(db, synced.user_id, ai_advisor.utcnow() - timedelta(days=7), ai_advisor.utcnow())
        db.rollback()
        after = [(w.worker_type, w.model_mode, w.primary_model, w.tier_name) for w in db.scalars(
            select(WorkerModelConfig).where(WorkerModelConfig.user_id == synced.user_id)
            .order_by(WorkerModelConfig.worker_type)).all()]
    assert before == after and versions(synced) == 2


def test_the_custom_classifier_plugin_routes_on_job_hunt_metadata():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "litellm"))
    try:
        import jobhunt_classifier as plugin
    finally:
        sys.path.pop(0)
    assert plugin.tier_for({"jobhunt": True, "worker_type": "INBOX_CLASSIFIER"}) == "SIMPLE"
    assert plugin.tier_for({"jobhunt": True, "worker_type": "RANKER", "escalation": "LOW_CONFIDENCE"}) == "REASONING"
    assert plugin.tier_for({"jobhunt": True, "worker_type": "FOLLOWUP", "previous_confidence": 0.4,
                            "confidence_required": 0.8}) == "COMPLEX"
    assert plugin.tier_for({"worker_type": "RANKER"}) is None  # not a Job Hunt OS request: decline
