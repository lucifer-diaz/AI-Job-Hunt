"""AI Orchestration: topology, node status, model assignment and history, live packets, job paths and traces.

Orchestration is a read-only projection, so these tests write business and telemetry rows directly (as the workers and
the instrumentation layer would) and read the API back. No model, LiteLLM or website is called. They run on the
default test database (SQLite or TEST_DATABASE_URL); every test uses its own fresh accounts.
"""
from __future__ import annotations

import json
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
from app.models import (
    AIConfigVersion,
    AIOptimizationReport,
    AppSetting,
    Application,
    ApplicationCommunicationEvent,
    ApplicationEvent,
    ExternalAPIRequestLog,
    FollowUp,
    Job,
    JobDescriptionFetchTask,
    JobDescriptionParseTask,
    JobRankingTask,
    LLMRequestLog,
    ResumeVariant,
    SystemLogEntry,
    TailorTask,
    WorkflowTransition,
)
from app.services import model_resolution, orchestration
from app.services import orchestration_topology as topo
from tests.conftest import make_user

SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz123456"
PROMPT_TEXT = "PRIVATE RESUME TEXT nobody should see"


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fresh_cache():
    orchestration.clear_cache()
    model_resolution.invalidate()
    yield
    orchestration.clear_cache()
    model_resolution.invalidate()


@pytest.fixture
def user_id():
    return make_user(SessionLocal)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid, is_admin=False):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", is_admin))
        return TestClient(app)
    return sign_in


def add_job(db, uid, **fields):
    values = dict(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Senior Backend Engineer",
                  company="Stripe", discovered_at=utcnow() - timedelta(hours=3), domain_match="MATCH",
                  domain_checked_at=utcnow() - timedelta(hours=3))
    values.update(fields)
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def llm_row(uid, **fields):
    values = dict(request_id=str(uuid.uuid4()), user_id=uid, stage="RANKER", provider="litellm",
                  requested_model="tier-2", model="ollama/gpt-oss:20b", prompt_name="ranking", prompt_version="v2",
                  input_tokens=4000, output_tokens=700, total_tokens=4700, usage_source="provider_reported",
                  latency_ms=8000, estimated_cost=Decimal(0), cost_source="free", status="SUCCEEDED", retry_count=0,
                  structured_output_valid=True, structured_output_retry_count=0, cached=False,
                  worker_type="RANKER", model_mode="TIER", configured_model="PRIMARY_REASONING",
                  created_at=utcnow() - timedelta(hours=1), completed_at=utcnow())
    values.update(fields)
    return LLMRequestLog(**values)


def snapshot(workers, tiers=None, routing=None, models=None):
    return {"schema": 1, "models": models or {}, "tiers": tiers or {}, "workers": workers,
            "routing": routing or {"auto_router_enabled": False, "auto_router_model_name": None,
                                   "classifier": "HEURISTIC", "tier_models": {}, "send_metadata": True}}


def gateway_mode(db, uid):
    db.add(AppSetting(user_id=uid, llm_provider="litellm", preferences={"ai": {"llm_provider": "litellm"}}))


def add_version(db, uid, number, snap, at, changed_by="me@jobhunt.test"):
    db.add(AIConfigVersion(user_id=uid, version_number=number, configuration_snapshot=snap, changed_by=changed_by,
                           source="USER", change_summary=f"v{number}", changes=[], created_at=at))


def scope(uid, days=7):
    now = utcnow()
    return orchestration.Scope(viewer_id=uid, user_id=uid, start=now - timedelta(days=days), end=now)


# ───────────── topology ─────────────

def test_the_topology_registry_is_consistent_and_honest_about_kinds():
    assert topo.validate() == []
    t = topo.public()
    nodes = {n["id"]: n for n in t["nodes"]}
    assert nodes["domain_filter"]["types"] == ["DETERMINISTIC"] and not nodes["domain_filter"]["workers"]
    assert nodes["ranker"]["primary_type"] == "AI" and nodes["ranker"]["workers"] == ["RANKER"]
    assert set(nodes["jd_recovery"]["types"]) >= {"EXTERNAL", "BROWSER", "AI", "ASYNC_WORKER"}
    assert nodes["jd_recovery"]["optional_ai"] is True
    assert nodes["approval"]["types"] == ["HUMAN_GATE"]
    assert nodes["discovery"]["primary_type"] == "EXTERNAL"
    # Job Insights is implemented: research is an async external stage with AI, preparation is AI only. Only the
    # Phase 7 follow-ups are planned, drawn as planned, and never in the measured graph.
    assert {r["id"] for r in t["reserved"]} == {"ats_correction_memory", "eval_feedback"}
    assert all(r["planned"] and r["position"] for r in t["reserved"])
    assert not {r["id"] for r in t["reserved"]} & set(nodes)
    assert nodes["submission_confirmation"]["types"] == ["DETERMINISTIC", "OUTCOME"]
    assert nodes["inbox_review"]["types"] == ["HUMAN_GATE"]
    assert set(nodes["company_insights"]["types"]) == {"ASYNC_WORKER", "EXTERNAL", "AI"}
    assert nodes["interview_insights"]["workers"] == ["INTERVIEW_INSIGHTS"]
    assert all(e["source"] in nodes and e["target"] in nodes for e in t["edges"])
    assert all(set(t["node_types"]) >= set(n["types"]) for n in t["nodes"])


def test_topology_endpoint_is_static_and_needs_no_data(signed_in, user_id):
    out = signed_in(user_id).get("/api/orchestration/topology").json()
    assert {"node_types", "nodes", "edges", "lanes"} <= set(out)


# ───────────── model assignment ─────────────

def test_fixed_tier_and_auto_router_assignments_come_from_the_active_version(user_id):
    with SessionLocal() as db:
        gateway_mode(db, user_id)
        add_version(db, user_id, 1, snapshot({"RANKER": {"mode": "FIXED", "primary_model": "phi-4"}}),
                    utcnow() - timedelta(days=3))
        add_version(db, user_id, 2, snapshot(
            {"RANKER": {"mode": "FIXED", "primary_model": "gpt-oss-20b", "fallback_model": "qwen-32b"},
             "TAILOR": {"mode": "TIER", "tier": "PRIMARY_REASONING"},
             "INBOX_CLASSIFIER": {"mode": "AUTO_ROUTER", "tier": "FAST"},
             "REVIEWER": {"mode": "TIER", "tier": "DEEP_REVIEW", "enabled": False}},
            tiers={"PRIMARY_REASONING": {"model": "tier-2", "enabled": True},
                   "FAST": {"model": "tier-1", "enabled": True}},
            routing={"auto_router_enabled": True, "auto_router_model_name": "jobhunt-auto", "classifier": "HEURISTIC",
                     "tier_models": {"SIMPLE": "llama3.2:3b", "COMPLEX": "gpt-oss-20b"}, "send_metadata": True}),
            utcnow() - timedelta(days=1))
        db.commit()
        workers = orchestration.assignments(db, user_id)["workers"]
    ranker, tailor, inbox = workers["RANKER"], workers["TAILOR"], workers["INBOX_CLASSIFIER"]
    assert (ranker["mode"], ranker["configured"], ranker["resolved_model"], ranker["fallback_model"]) == \
        ("FIXED", "gpt-oss-20b", "gpt-oss-20b", "qwen-32b")
    assert ranker["config_version"] == 2
    assert (tailor["mode"], tailor["configured"], tailor["resolved_model"]) == ("TIER", "PRIMARY_REASONING", "tier-2")
    assert inbox["mode"] == "AUTO_ROUTER" and inbox["resolved_model"] == "jobhunt-auto"
    assert inbox["router"]["active"] is True and inbox["router"]["tiers"]["SIMPLE"] == "llama3.2:3b"
    assert workers["REVIEWER"]["enabled"] is False


def test_direct_provider_mode_reports_the_settings_stage_model(user_id):
    with SessionLocal() as db:
        db.add(AppSetting(user_id=user_id, llm_provider="openrouter",
                          preferences={"ai": {"llm_provider": "openrouter", "model": "vendor/default",
                                              "stage_models": {"rank": "vendor/rank"}}}))
        db.commit()
        workers = orchestration.assignments(db, user_id)["workers"]
    assert workers["RANKER"]["mode"] == "DIRECT" and workers["RANKER"]["resolved_model"] == "vendor/rank"
    assert workers["JD_PARSER"]["resolved_model"] == "vendor/default"


def test_configured_requested_and_actual_models_stay_separate_and_unknown_is_never_inferred(user_id):
    with SessionLocal() as db:
        db.add_all([llm_row(user_id, configured_model="AUTO_ROUTER", model_mode="AUTO_ROUTER",
                            requested_model="jobhunt-auto", model="ollama/gpt-oss:20b", routing_tier="COMPLEX"),
                    llm_row(user_id, configured_model="AUTO_ROUTER", model_mode="AUTO_ROUTER",
                            requested_model="jobhunt-auto", model=None, routing_tier=None)])
        db.commit()
        rows = orchestration.llm_rows(db, scope(user_id), ["RANKER"])
    detail = orchestration.routing_detail(rows, [])
    assert detail["requested"] == {"jobhunt-auto": 2}
    assert detail["actual"] == {"ollama/gpt-oss:20b": 1} and detail["actual_unknown"] == 1
    assert detail["tiers_recorded"] is True and detail["tiers"] == [{"tier": "COMPLEX", "requests": 1, "share": 1.0}]
    card = orchestration.llm_card(rows[0])
    assert {"configured_model", "requested_model", "actual_model"} <= set(card)


def test_no_routing_metadata_is_reported_as_such(user_id):
    with SessionLocal() as db:
        db.add(llm_row(user_id, configured_model="AUTO_ROUTER", model_mode="AUTO_ROUTER", routing_tier=None))
        db.commit()
        detail = orchestration.routing_detail(orchestration.llm_rows(db, scope(user_id), ["RANKER"]), [])
    assert detail["auto_router_requests"] == 1 and detail["tiers_recorded"] is False and detail["tiers"] == []


# ───────────── metrics and node status ─────────────

def test_node_metrics_respect_the_range_and_count_fallbacks_and_schema_failures(user_id):
    with SessionLocal() as db:
        db.add_all([llm_row(user_id, latency_ms=1000), llm_row(user_id, latency_ms=3000, fallback_used=True),
                    llm_row(user_id, status="FAILED", error_type="SCHEMA_VALIDATION", structured_output_valid=False,
                            fallback_used=False, estimated_cost=None, cost_source="unknown"),
                    llm_row(user_id, created_at=utcnow() - timedelta(days=20))])  # outside the 7-day range
        db.commit()
        g = orchestration.graph(db, scope(user_id))
    llm = g["nodes"]["ranker"]["llm"]
    assert llm["requests"] == 3 and llm["failed"] == 1 and llm["success_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert llm["fallbacks"] == 1 and llm["fallback_rate"] == 0.5
    assert llm["schema_failures"] == 1 and llm["unpriced_requests"] == 1 and llm["estimated_cost"] == 0.0
    assert llm["low_sample"] is True
    assert g["nodes"]["ranker"]["observed_models"] == ["ollama/gpt-oss:20b"]


def test_one_historic_failure_never_makes_a_node_error(user_id):
    with SessionLocal() as db:
        db.add_all([llm_row(user_id, status="FAILED", error_type="TIMEOUT", created_at=utcnow() - timedelta(days=3)),
                    llm_row(user_id, created_at=utcnow() - timedelta(hours=2))])
        db.commit()
        status = orchestration.graph(db, scope(user_id))["nodes"]["ranker"]["status"]
    assert status == "HEALTHY"


def test_consecutive_recent_failures_make_a_node_error_and_a_failure_rate_degrades_it(user_id):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        db.add_all([llm_row(user_id, stage="TAILOR", worker_type="TAILOR", status="FAILED", error_type="TIMEOUT",
                            created_at=utcnow() - timedelta(minutes=m)) for m in (1, 2, 3)])
        db.add_all([llm_row(other, created_at=utcnow() - timedelta(minutes=m)) for m in range(1, 10)]
                   + [llm_row(other, status="FAILED", error_type="TIMEOUT", created_at=utcnow() - timedelta(minutes=30))])
        db.commit()
        tailor = orchestration.graph(db, scope(user_id))["nodes"]["tailor"]
        ranker = orchestration.graph(db, scope(other))["nodes"]["ranker"]
    assert tailor["status"] == "ERROR" and "3 model calls" in tailor["status_reasons"][0]
    assert ranker["status"] == "DEGRADED" and "10%" in ranker["status_reasons"][0]


def test_nodes_without_data_say_so_and_disabled_workers_are_disabled(user_id):
    with SessionLocal() as db:
        gateway_mode(db, user_id)
        add_version(db, user_id, 1, snapshot({"REVIEWER": {"mode": "TIER", "tier": "DEEP_REVIEW", "enabled": False},
                                              "RANKER": {"mode": "TIER", "tier": "MISSING"}}), utcnow())
        db.commit()
        nodes = orchestration.graph(db, scope(user_id))["nodes"]
    assert nodes["reviewer"]["status"] == "DISABLED"
    assert nodes["domain_filter"]["status"] == "NO_DATA"
    # An enabled worker no model can be resolved for cannot run: degraded, with the reason.
    assert nodes["ranker"]["status"] == "DEGRADED"
    assert any("No model can be resolved for Ranker" in r for r in nodes["ranker"]["status_reasons"])


def test_queues_make_nodes_busy_and_live_counts_follow_task_tables(user_id):
    with SessionLocal() as db:
        a, b, c = (add_job(db, user_id) for _ in range(3))
        db.add_all([JobRankingTask(user_id=user_id, job_id=a.id, status="PROCESSING", started_at=utcnow()),
                    JobDescriptionParseTask(user_id=user_id, job_id=b.id, description_hash="x", status="PENDING",
                                            attempt_count=1, last_error="timeout"),
                    TailorTask(user_id=user_id, job_id=c.id, status="running", stage="reviewing", attempts=1,
                               started_at=utcnow())])
        db.commit()
        g = orchestration.graph(db, scope(user_id))
    assert g["nodes"]["ranker"]["live"]["running"] == 1 and g["nodes"]["ranker"]["status"] == "BUSY"
    assert g["nodes"]["jd_parser"]["live"]["retrying"] == 1
    assert g["nodes"]["reviewer"]["live"]["running"] == 1
    assert g["health_policy"]["rules"][0]["status"] == "DISABLED"


# ───────────── edges ─────────────

def test_ranker_branches_are_counted_from_transitions_with_shares(user_id, signed_in):
    with SessionLocal() as db:
        for target in ("QUALIFIED", "QUALIFIED", "ARCHIVED"):
            job = add_job(db, user_id)
            db.add(WorkflowTransition(job_id=job.id, user_id=user_id, from_state="RANKED", to_state=target,
                                      actor="ranking-policy", reason="policy", created_at=utcnow() - timedelta(hours=1)))
        db.commit()
    client = signed_in(user_id)
    g = client.get("/api/orchestration/graph").json()
    assert g["edges"]["ranker__tailor"]["count"] == 2 and g["edges"]["ranker__archived"]["count"] == 1
    detail = client.get("/api/orchestration/edges/ranker/tailor").json()
    assert {s["target"]: s["share"] for s in detail["split"]} == {"tailor": pytest.approx(0.6667, abs=1e-3),
                                                                  "archived": pytest.approx(0.3333, abs=1e-3)}
    assert detail["timing_note"] and detail["median_seconds"] is None  # no per-item time is stored for it
    assert detail["recent"][0]["title"] == "Senior Backend Engineer"
    assert client.get("/api/orchestration/edges/ranker/nowhere").status_code == 404


def test_timed_edges_use_both_stored_timestamps(user_id):
    with SessionLocal() as db:
        job = add_job(db, user_id)
        approved = utcnow() - timedelta(hours=2)
        db.add(Application(job_id=job.id, user_id=user_id, status="applied", approved_at=approved,
                           applied_at=approved + timedelta(minutes=30)))
        db.commit()
        detail = orchestration.edge_detail(db, scope(user_id), "approval", "application")
    assert detail["count"] == 1 and detail["median_seconds"] == 1800


# ───────────── model history ─────────────

def test_model_history_follows_versions_and_reports_observed_metrics_per_period(user_id, signed_in):
    now = utcnow()
    with SessionLocal() as db:
        gateway_mode(db, user_id)
        add_version(db, user_id, 1, snapshot({"RANKER": {"mode": "FIXED", "primary_model": "phi-4"}}),
                    now - timedelta(days=20))
        add_version(db, user_id, 2, snapshot({"RANKER": {"mode": "FIXED", "primary_model": "qwen-14b"}}),
                    now - timedelta(days=10))
        # A change to another worker does not start a new Ranker period.
        add_version(db, user_id, 3, snapshot({"RANKER": {"mode": "FIXED", "primary_model": "qwen-14b"},
                                              "TAILOR": {"mode": "FIXED", "primary_model": "x"}}),
                    now - timedelta(days=8))
        add_version(db, user_id, 4, snapshot({"RANKER": {"mode": "FIXED", "primary_model": "gpt-oss-20b"}}),
                    now - timedelta(days=2))
        db.add_all([llm_row(user_id, model="phi-4", created_at=now - timedelta(days=15))]
                   + [llm_row(user_id, model="qwen-14b", created_at=now - timedelta(days=5)) for _ in range(2)]
                   + [llm_row(user_id, model="gpt-oss-20b", created_at=now - timedelta(days=1), latency_ms=7000)])
        db.commit()
    out = signed_in(user_id).get("/api/orchestration/nodes/ranker/history?range=30d").json()
    ranker = out["workers"][0]
    assert [p["model"] for p in ranker["periods"]] == ["gpt-oss-20b", "qwen-14b", "phi-4"]
    current, previous, first = ranker["periods"]
    assert current["current"] is True and current["ended_at"] is None and current["from_version"] == 4
    assert (previous["from_version"], previous["to_version"]) == (2, 3)
    assert previous["observed"]["requests"] == 2 and previous["observed"]["low_sample"] is True
    assert first["observed"]["requests"] == 1 and first["observed_models"] == {"phi-4": 1}
    assert [c["version"] for c in ranker["changes"]] == [4, 2]
    assert ranker["changes"][0]["from"]["model"] == "qwen-14b" and ranker["changes"][0]["to"]["model"] == "gpt-oss-20b"
    assert {m["model"] for m in ranker["by_actual_model"]} == {"phi-4", "qwen-14b", "gpt-oss-20b"}
    assert "not proof" in out["note"]


# ───────────── live packets and job state ─────────────

def _position(db, job):
    return orchestration.locate(orchestration.load_facts(db, job, job.user_id))


def test_current_stage_is_derived_from_the_most_specific_live_fact(user_id):
    with SessionLocal() as db:
        ranking = add_job(db, user_id, state="RANKING")
        db.add(JobRankingTask(user_id=user_id, job_id=ranking.id, status="PROCESSING", attempt_count=1,
                              started_at=utcnow()))
        retrying = add_job(db, user_id, state="NORMALIZED")
        db.add(JobDescriptionFetchTask(user_id=user_id, job_id=retrying.id, status="PENDING", attempt_count=2,
                                       last_error=f"timeout with key {SECRET}"))
        reviewing = add_job(db, user_id, state="QUALIFIED")
        db.add(TailorTask(user_id=user_id, job_id=reviewing.id, status="running", stage="reviewing", attempts=1))
        waiting = add_job(db, user_id, state="QUALIFIED", status="pending_approval")
        applied = add_job(db, user_id, state="QUALIFIED", status="applied")
        db.add(Application(job_id=applied.id, user_id=user_id, status="applied", stage="applied",
                           approved_at=utcnow(), applied_at=utcnow()))
        offer = add_job(db, user_id, state="QUALIFIED", status="applied")
        db.add(Application(job_id=offer.id, user_id=user_id, status="applied", stage="offer", applied_at=utcnow()))
        archived = add_job(db, user_id, state="ARCHIVED", previous_state="RANKED")
        failed = add_job(db, user_id, state="FAILED", previous_state="RANKED", last_error="boom")
        hidden = add_job(db, user_id, domain_match="NOT_MATCHING", domain_reason="Outside your roles")
        db.commit()
        p = {name: _position(db, job) for name, job in (
            ("ranking", ranking), ("retrying", retrying), ("reviewing", reviewing), ("waiting", waiting),
            ("applied", applied), ("offer", offer), ("archived", archived), ("failed", failed), ("hidden", hidden))}
    assert (p["ranking"].node_id, p["ranking"].activity, p["ranking"].attempt) == ("ranker", "RUNNING", 1)
    assert (p["retrying"].node_id, p["retrying"].activity, p["retrying"].attempt) == ("jd_recovery", "RETRYING", 2)
    assert SECRET not in (p["retrying"].reason or "")
    assert (p["reviewing"].node_id, p["reviewing"].activity) == ("reviewer", "RUNNING")
    assert (p["waiting"].node_id, p["waiting"].activity) == ("approval", "AWAITING_HUMAN")
    assert (p["applied"].node_id, p["applied"].activity) == ("application_inbox", "MONITORING")
    assert p["offer"].terminal and p["offer"].terminal_node == "outcome"
    assert p["archived"].terminal and p["archived"].terminal_node == "archived"
    assert (p["failed"].node_id, p["failed"].activity) == ("ranker", "FAILED")
    assert (p["hidden"].node_id, p["hidden"].activity) == ("domain_filter", "BLOCKED")


def test_job_path_marks_completed_current_and_waiting_stages(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="QUALIFIED", resume_id=None)
        db.add(JobDescriptionParseTask(user_id=user_id, job_id=job.id, description_hash="h", status="COMPLETED",
                                       started_at=utcnow() - timedelta(hours=2), completed_at=utcnow() - timedelta(hours=2)))
        db.add(TailorTask(user_id=user_id, job_id=job.id, status="running", stage="tailoring", attempts=1,
                          started_at=utcnow() - timedelta(seconds=18)))
        db.add(llm_row(user_id, stage="TAILOR", worker_type="TAILOR", workflow_type="job", workflow_entity_id=job.id,
                       model="gpt-oss:20b", created_at=utcnow() - timedelta(seconds=5)))
        db.commit()
        job_id = job.id
    out = signed_in(user_id).get(f"/api/orchestration/jobs/{job_id}").json()
    steps = {s["node_id"]: s for s in out["path"]}
    assert steps["discovery"]["status"] == "COMPLETED" and steps["jd_parser"]["status"] == "COMPLETED"
    assert steps["tailor"]["status"] == "CURRENT" and steps["tailor"]["current"] is True
    assert steps["reviewer"]["status"] == "WAITING" and steps["approval"]["status"] == "WAITING"
    assert "archived" not in steps and "interview" not in steps  # branches appear only when taken
    packet = out["packet"]
    assert (packet["node_id"], packet["activity"], packet["model"], packet["model_source"]) == \
        ("tailor", "RUNNING", "gpt-oss:20b", "telemetry")
    assert packet["attempt"] == 1 and packet["max_attempts"] == 2 and packet["elapsed_seconds"] >= 17
    assert packet["active"] is True


def test_a_running_packet_without_telemetry_shows_no_invented_model(user_id):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="RANKING")
        db.add(JobRankingTask(user_id=user_id, job_id=job.id, status="PROCESSING", attempt_count=1, started_at=utcnow()))
        db.commit()
        packet = orchestration.packet(db, job, user_id)
    # Direct mock provider: the assignment is the Settings model, labelled as the assignment, never as what answered.
    assert packet["model_source"] in ("assignment", None)


def test_terminal_and_failed_paths(user_id):
    with SessionLocal() as db:
        archived = add_job(db, user_id, state="ARCHIVED", previous_state="RANKED")
        db.add(WorkflowTransition(job_id=archived.id, user_id=user_id, from_state="RANKED", to_state="ARCHIVED",
                                  actor="ranking-policy", reason="Fit 41 is below your floor of 65"))
        failed = add_job(db, user_id, state="FAILED", previous_state="TAILORING", last_error="model timeout")
        db.add(TailorTask(user_id=user_id, job_id=failed.id, status="failed", stage="done", error="timeout",
                          finished_at=utcnow()))
        db.commit()
        arch = orchestration.job_state(db, archived.id, user_id)
        fail = orchestration.job_state(db, failed.id, user_id)
    steps = {s["node_id"]: s for s in arch["path"]}
    assert steps["archived"]["status"] == "COMPLETED" and steps["archived"]["terminal"] is True
    assert steps["tailor"]["status"] == "SKIPPED" and arch["packet"]["terminal"] is True
    fsteps = {s["node_id"]: s for s in fail["path"]}
    assert fsteps["tailor"]["status"] == "FAILED" and fsteps["tailor"]["current"] is True
    assert fail["packet"]["activity"] == "FAILED"


def test_live_lists_active_packets_and_polling_stops_when_nothing_runs(user_id, signed_in):
    client = signed_in(user_id)
    assert client.get("/api/orchestration/live").json()["active"] is False
    with SessionLocal() as db:
        a, b = add_job(db, user_id, title="AI Engineer", company="Grab"), add_job(db, user_id, company="Canva")
        db.add_all([JobRankingTask(user_id=user_id, job_id=a.id, status="PROCESSING", attempt_count=1,
                                   started_at=utcnow() - timedelta(seconds=7)),
                    JobDescriptionParseTask(user_id=user_id, job_id=b.id, description_hash="h", status="PENDING")])
        db.add(llm_row(user_id, workflow_type="job", workflow_entity_id=a.id, model="ollama/gpt-oss:20b",
                       created_at=utcnow()))
        db.commit()
    out = client.get("/api/orchestration/live").json()
    assert out["active"] is True and [p["activity"] for p in out["packets"]] == ["RUNNING", "QUEUED"]
    first = out["packets"][0]
    assert (first["company"], first["node_id"], first["model"]) == ("Grab", "ranker", "ollama/gpt-oss:20b")
    assert out["counts"]["jd_parser"]["queued"] == 1
    summary = client.get("/api/orchestration/summary").json()
    assert summary["running"] == 1 and summary["queued"] == 1


def test_job_search_is_bounded_and_matches_id_title_and_company(user_id, signed_in):
    with SessionLocal() as db:
        for i in range(30):
            add_job(db, user_id, title=f"Engineer {i}", company="Initech")
        target = add_job(db, user_id, title="Staff SWE", company="Canva")
        db.commit()
        target_id = target.id
    client = signed_in(user_id)
    assert len(client.get("/api/orchestration/jobs?q=initech&limit=50").json()["items"]) == 30
    assert len(client.get("/api/orchestration/jobs?q=initech").json()["items"]) == 20
    assert client.get(f"/api/orchestration/jobs?q=%23{target_id}").json()["items"][0]["company"] == "Canva"
    assert client.get("/api/orchestration/jobs?limit=500").status_code == 422


# ───────────── trace ─────────────

def test_job_trace_merges_every_source_in_order_without_payloads(user_id, signed_in):
    t0 = utcnow() - timedelta(hours=2)
    trace = uuid.uuid4().hex
    with SessionLocal() as db:
        job = add_job(db, user_id, discovered_at=t0, state="QUALIFIED")
        db.add_all([
            WorkflowTransition(job_id=job.id, user_id=user_id, from_state="DISCOVERED", to_state="NORMALIZED",
                               actor="discovery", created_at=t0 + timedelta(seconds=2)),
            WorkflowTransition(job_id=job.id, user_id=user_id, from_state="RANKED", to_state="QUALIFIED",
                               actor="ranking-policy", created_at=t0 + timedelta(seconds=30)),
            JobRankingTask(user_id=user_id, job_id=job.id, status="COMPLETED", attempt_count=1,
                           created_at=t0 + timedelta(seconds=10), started_at=t0 + timedelta(seconds=12),
                           completed_at=t0 + timedelta(seconds=25)),
            llm_row(user_id, workflow_type="job", workflow_entity_id=job.id, trace_id=trace,
                    created_at=t0 + timedelta(seconds=13), debug_prompt=PROMPT_TEXT, debug_response=PROMPT_TEXT),
            ExternalAPIRequestLog(request_id=str(uuid.uuid4()), user_id=user_id, provider="WEB", service="job_posting",
                                  operation="jd_recovery_http", job_id=job.id, status="FAILED", error_type="TIMEOUT",
                                  trace_id=trace, latency_ms=20000, created_at=t0 + timedelta(seconds=5)),
            SystemLogEntry(level="ERROR", category="SCHEDULER", message=f"ranking run failed key={SECRET}",
                           trace_id=trace, user_id=user_id, created_at=t0 + timedelta(seconds=26)),
        ])
        db.commit()
        job_id = job.id
    client = signed_in(user_id)
    out = client.get(f"/api/orchestration/jobs/{job_id}/trace").json()
    times = [e["at"] for e in out["events"]]
    assert times == sorted(times)
    sources = [e["source"] for e in out["events"]]
    assert {"workflow", "task", "llm", "external", "log"} <= set(sources)
    llm = next(e for e in out["events"] if e["source"] == "llm")["llm"]
    assert llm["actual_model"] == "ollama/gpt-oss:20b" and llm["requested_model"] == "tier-2"
    assert out["trace_ids"] == [trace]
    body = json.dumps(out)
    assert PROMPT_TEXT not in body and SECRET not in body


def test_other_accounts_jobs_are_invisible(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        theirs = add_job(db, other, title="Secret Role", company="Hooli")
        db.add(JobRankingTask(user_id=other, job_id=theirs.id, status="PROCESSING", started_at=utcnow()))
        db.add(llm_row(other))
        db.commit()
        their_id = theirs.id
    client = signed_in(user_id)
    assert client.get(f"/api/orchestration/jobs/{their_id}").status_code == 404
    assert client.get(f"/api/orchestration/jobs/{their_id}/trace").status_code == 404
    assert client.get(f"/api/orchestration/jobs/{their_id}/live").status_code == 404
    assert "Hooli" not in json.dumps(client.get("/api/orchestration/jobs?q=hooli").json())
    live = client.get("/api/orchestration/live").json()
    assert live["packets"] == [] and live["counts"]["ranker"]["running"] == 0
    assert client.get("/api/orchestration/graph").json()["nodes"]["ranker"]["llm"]["requests"] == 0
    assert client.get("/api/orchestration/graph?all_accounts=true").status_code == 403


def test_admin_every_account_view_is_aggregate_only(user_id, signed_in):
    other = make_user(SessionLocal)
    with SessionLocal() as db:
        theirs = add_job(db, other, title="Secret Role", company="Hooli")
        db.add(JobRankingTask(user_id=other, job_id=theirs.id, status="PROCESSING", started_at=utcnow()))
        db.add_all([llm_row(other, status="FAILED", error_type="TIMEOUT", workflow_type="job",
                            workflow_entity_id=theirs.id)])
        db.commit()
    admin = signed_in(user_id, is_admin=True)
    g = admin.get("/api/orchestration/graph?all_accounts=true").json()
    assert g["scope"]["all_accounts"] is True and g["nodes"]["ranker"]["llm"]["requests"] >= 1
    live = admin.get("/api/orchestration/live?all_accounts=true").json()
    assert live["counts"]["ranker"]["running"] >= 1
    assert all(p.get("company") != "Hooli" for p in live["packets"])  # packets stay the admin's own
    node = admin.get("/api/orchestration/nodes/ranker?all_accounts=true").json()
    recent = node["recent"]  # counts only: no call, request, task or job of another account, not even id-less
    assert recent["aggregate_only"] is True and recent["calls"]["failed"] >= 1
    assert recent["failures_by_error"].get("TIMEOUT", 0) >= 1
    assert not {"failures", "successes", "retries", "failed_tasks"} & set(recent)
    assert "Hooli" not in json.dumps(node) and "Secret Role" not in json.dumps(g)


# ───────────── node drawer, recommendations, security ─────────────

def test_node_detail_has_routing_recent_activity_and_matching_recommendations(user_id, signed_in):
    with SessionLocal() as db:
        db.add_all([llm_row(user_id, fallback_used=True, escalated=True, escalation_reason="LOW_CONFIDENCE"),
                    llm_row(user_id, status="FAILED", error_type="TIMEOUT", retry_count=1, fallback_used=False)])
        db.add(AIOptimizationReport(user_id=user_id, time_range_start=utcnow() - timedelta(days=7),
                                    time_range_end=utcnow(), recommendations=[
                                        {"title": "Assign Ranker directly", "affected_stage": "RANKER",
                                         "priority": "HIGH", "category": "MODEL"},
                                        {"title": "Shorten tailor prompt", "affected_stage": "TAILOR"}]))
        job = add_job(db, user_id)
        db.add(JobRankingTask(user_id=user_id, job_id=job.id, status="FAILED", error_category="TIMEOUT",
                              last_error=f"provider said Authorization: Bearer {SECRET}", attempt_count=3,
                              completed_at=utcnow()))
        db.commit()
    client = signed_in(user_id)
    out = client.get("/api/orchestration/nodes/ranker").json()
    assert out["routing"]["fallback_requests"] == 1 and out["routing"]["escalations"] == 1
    assert out["routing"]["escalation_reasons"] == {"LOW_CONFIDENCE": 1}
    assert len(out["recent"]["failures"]) == 1 and len(out["recent"]["retries"]) == 2
    assert [r["title"] for r in out["recommendations"]] == ["Assign Ranker directly"]
    assert client.get("/api/orchestration/graph").json()["nodes"]["ranker"]["recommendations"] == 1
    assert out["recent"]["failed_tasks"][0]["error_category"] == "TIMEOUT"
    assert SECRET not in json.dumps(out)
    assert client.get("/api/orchestration/nodes/company_insights").status_code == 200
    assert client.get("/api/orchestration/nodes/nope").status_code == 404


def test_orchestration_payloads_never_carry_prompts_or_secrets(user_id, signed_in):
    with SessionLocal() as db:
        job = add_job(db, user_id, state="QUALIFIED", status="applied")
        app_row = Application(job_id=job.id, user_id=user_id, status="applied", stage="interviewing",
                              applied_at=utcnow() - timedelta(days=2))
        db.add(app_row)
        db.flush()
        db.add(ApplicationEvent(application_id=app_row.id, user_id=user_id, kind="stage", title="Moved to Interviewing",
                                detail="interviewing", occurred_at=utcnow() - timedelta(days=1)))
        # An inbox event's title is built from the email's sender (application_inbox.analysis._mirror).
        db.add(ApplicationEvent(application_id=app_row.id, user_id=user_id, kind="email",
                                title="Interview invitation from Jane Recruiter", detail="",
                                occurred_at=utcnow() - timedelta(hours=20)))
        db.add(ApplicationCommunicationEvent(user_id=user_id, application_id=app_row.id, gmail_message_id="m1",
                                             event_type="INTERVIEW_INVITATION", subject="Interview at Stripe — secret",
                                             sender="recruiter@stripe.com", summary=PROMPT_TEXT,
                                             received_at=utcnow() - timedelta(days=1)))
        db.add(FollowUp(application_id=app_row.id, user_id=user_id, due_at=utcnow() + timedelta(days=3),
                        draft=PROMPT_TEXT, status="draft"))
        db.add(ResumeVariant(job_id=job.id, user_id=user_id, version=1, markdown=PROMPT_TEXT, cover_letter=PROMPT_TEXT,
                             reviewer_score=82, reviewer_notes={"decision": "PASS", "summary": PROMPT_TEXT}))
        db.add(llm_row(user_id, workflow_type="job", workflow_entity_id=job.id, debug_prompt=PROMPT_TEXT,
                       error_message=SECRET))
        db.commit()
        job_id = job.id
    client = signed_in(user_id)
    bodies = [client.get(url).json() for url in (
        "/api/orchestration/graph", "/api/orchestration/live", f"/api/orchestration/jobs/{job_id}",
        f"/api/orchestration/jobs/{job_id}/trace", "/api/orchestration/nodes/ranker",
        "/api/orchestration/nodes/reviewer", "/api/orchestration/nodes/application_inbox",
        "/api/orchestration/nodes/ranker/history")]
    text = json.dumps(bodies)
    for private in (PROMPT_TEXT, SECRET, "recruiter@stripe.com", "Interview at Stripe", "Jane Recruiter"):
        assert private not in text
    steps = {s["node_id"]: s for s in bodies[2]["path"]}
    assert steps["interview"]["status"] == "CURRENT" and steps["reviewer"]["status"] == "COMPLETED"
    assert bodies[6]["quality"]["events"] == 1


def test_graph_is_cached_briefly_and_bounded(user_id):
    with SessionLocal() as db:
        first = orchestration.graph(db, scope(user_id))
        db.add(llm_row(user_id))
        db.commit()
        assert orchestration.graph(db, scope(user_id))["nodes"]["ranker"]["llm"]["requests"] == \
            first["nodes"]["ranker"]["llm"]["requests"]
        orchestration.clear_cache()
        assert orchestration.graph(db, scope(user_id))["nodes"]["ranker"]["llm"]["requests"] == \
            first["nodes"]["ranker"]["llm"]["requests"] + 1


def test_custom_windows_never_share_a_cached_graph(user_id, signed_in):
    client = signed_in(user_id)
    with SessionLocal() as db:
        db.add(llm_row(user_id, created_at=utcnow() - timedelta(hours=1)))
        db.commit()
    start = (utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat()
    wide = client.get(f"/api/orchestration/graph?start={start}").json()
    narrow = client.get(f"/api/orchestration/graph?start={start}&end={(utcnow() - timedelta(hours=2)).isoformat()}").json()
    assert wide["nodes"]["ranker"]["llm"]["requests"] >= 1 and narrow["nodes"]["ranker"]["llm"]["requests"] == 0


def test_orchestration_never_writes(user_id, signed_in):
    """Every endpoint is a GET, and reading the whole workspace changes nothing."""
    from app.orchestration_api import router

    assert router.routes and all(r.methods == {"GET"} for r in router.routes)
    with SessionLocal() as db:
        job = add_job(db, user_id)
        db.commit()
        job_id = job.id
        before = (db.scalar(select(Job.updated_at).where(Job.id == job_id)),
                  len(db.scalars(select(AIConfigVersion).where(AIConfigVersion.user_id == user_id)).all()))
    client = signed_in(user_id)
    for url in ("/api/orchestration/graph", "/api/orchestration/live", f"/api/orchestration/jobs/{job_id}",
                f"/api/orchestration/jobs/{job_id}/trace", "/api/orchestration/nodes/tailor/history"):
        assert client.get(url).status_code == 200
    with SessionLocal() as db:
        after = (db.scalar(select(Job.updated_at).where(Job.id == job_id)),
                 len(db.scalars(select(AIConfigVersion).where(AIConfigVersion.user_id == user_id)).all()))
    assert before == after
