"""Phase 7: one application journey correlated end to end from stored facts, and the rules that keep it honest.

The journey is a read model (app.services.journey), so these tests write business and telemetry rows directly, as the
workers and the instrumentation layer would, and read the API back. Nothing calls a model, LiteLLM, Gmail or a site.
SQLite or TEST_DATABASE_URL; every test uses fresh accounts.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import (
    Application,
    ApplicationClaim,
    ApplicationCommunicationEvent,
    ApplicationEmailMessage,
    ApplicationExecutionTask,
    ApprovedPacket,
    ExternalAPIRequestLog,
    Job,
    JobDescriptionParseTask,
    LLMRequestLog,
    ResumeVariant,
    SubmissionIntent,
)
from app.services import (  # noqa: F401  (registers the stage guard)
    application_execution,
    journey,
    llm,
    model_performance,
    monitoring,
    tracing,
)
from app.services.llm_telemetry import LLMCall
from tests.conftest import make_user

PRIVATE = ("PRIVATE POSTING TEXT", "PRIVATE RESUME", "recruiter@globex.test", "Your application was rejected",
           "PRIVATE EMAIL BODY", "sk-live-abcdefghijklmnopqrstuvwxyz123456")


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def signed_in(monkeypatch):
    def sign_in(uid):
        monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(uid, f"{uid}@jobhunt.test", False))
        return TestClient(app)
    return sign_in


def llm_row(uid, job_id, t, **fields):
    values = {"request_id": str(uuid.uuid4()), "user_id": uid, "stage": "RANKER", "provider": "litellm",
              "requested_model": "ranker-main", "model": "ollama/gpt-oss:20b", "model_group": "ranker-main",
              "configured_model": "ranker-main", "prompt_name": "ranking", "prompt_version": "v2",
              "input_tokens": 4000, "output_tokens": 700, "total_tokens": 4700, "usage_source": "provider_reported",
              "latency_ms": 8000, "estimated_cost": Decimal("0.0200"), "cost_source": "provider_reported",
              "status": "SUCCEEDED", "retry_count": 0, "structured_output_valid": True,
              "structured_output_retry_count": 0, "cached": False, "worker_type": "RANKER", "model_mode": "FIXED",
              "workflow_type": "job", "workflow_entity_id": job_id, "trace_id": "a" * 32, "created_at": t,
              "completed_at": t + timedelta(seconds=8)}
    values.update(fields)
    return LLMRequestLog(**values)


def ext_row(uid, job_id, t, request_id=None, **fields):
    values = {"request_id": request_id or str(uuid.uuid4()), "user_id": uid, "provider": "LITELLM", "service": "chat",
              "operation": "chat_completion", "job_id": job_id, "status": "SUCCEEDED", "latency_ms": 8000,
              "trace_id": "a" * 32, "created_at": t}
    values.update(fields)
    return ExternalAPIRequestLog(**values)


def seed_journey(uid: int, *, gmail: str = "MATCHED") -> dict[str, int]:
    """Discovery → parse → retrieval → ranking (with a fallback) → tailoring → approval → guarded execution →
    CONFIRMED intent → a Gmail reply, every row as its owner writes it."""
    t0 = utcnow() - timedelta(hours=6)
    with SessionLocal() as db:
        db.info["user_id"] = uid
        job = Job(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Backend Engineer", company="Globex",
                  description=PRIVATE[0], discovered_at=t0, domain_match="MATCH", source_board="greenhouse",
                  description_source="SOURCE", description_quality="GOOD")
        db.add(job)
        db.flush()
        db.add(JobDescriptionParseTask(user_id=uid, job_id=job.id, description_hash="h" * 64, status="COMPLETED",
                                       created_at=t0 + timedelta(minutes=1), completed_at=t0 + timedelta(minutes=2)))
        linked = str(uuid.uuid4())
        db.add_all([
            # Ranking: retrieval timed on the call, cost reported by the gateway, and its own external row.
            llm_row(uid, job.id, t0 + timedelta(minutes=5), retrieval_ms=37, retrieval_mode="on",
                    external_request_id=linked),
            ext_row(uid, job.id, t0 + timedelta(minutes=5), request_id=linked, estimated_cost=Decimal("0.0200")),
            # Tailoring: the gateway fell back to another model group.
            llm_row(uid, job.id, t0 + timedelta(minutes=30), stage="TAILOR", worker_type="TAILOR",
                    configured_model="tailor-main", requested_model="tailor-main", model_group="tailor-backup",
                    model="openai/gpt-4.1-mini", fallback_used=True, estimated_cost=None, cost_source="unknown"),
            # An embedding call for this job: an external row of its own, with LiteLLM's reported cost.
            ext_row(uid, job.id, t0 + timedelta(minutes=4), service="embedding", operation="embed_query",
                    estimated_cost=Decimal("0.0001")),
        ])
        variant = ResumeVariant(user_id=uid, job_id=job.id, version=2, markdown=PRIVATE[1], reviewer_score=0.86,
                                created_at=t0 + timedelta(minutes=40))
        db.add(variant)
        db.flush()
        application = Application(user_id=uid, job_id=job.id, status="applied", approved_at=t0 + timedelta(hours=1),
                                  applied_at=t0 + timedelta(hours=2), submitted_via="worker",
                                  confirmation_id="AC-12345", stage="rejected", recruiter_email=PRIVATE[2])
        db.add(application)
        db.flush()
        packet = ApprovedPacket(user_id=uid, application_id=application.id, job_id=job.id, variant_id=variant.id,
                                variant_version=2, serializer_version=1, digest="d" * 64,
                                content={"resume_markdown": PRIVATE[1]}, created_at=t0 + timedelta(hours=1))
        db.add(packet)
        db.flush()
        task = ApplicationExecutionTask(user_id=uid, job_id=job.id, application_id=application.id,
                                        approved_packet_id=packet.id, mode="GUARDED_AUTO", status="COMPLETED",
                                        outcome="CONFIRMED", backend="playwright",
                                        route="greenhouse:globex", created_at=t0 + timedelta(hours=1, minutes=1),
                                        started_at=t0 + timedelta(hours=1, minutes=2),
                                        completed_at=t0 + timedelta(hours=2))
        # As application_execution.set_stage writes it for the guarded run that holds the intent (the column guard
        # refuses CONFIRMED from anywhere else).
        task._submit_stage_allowed = "CONFIRMED"
        task.stage = "CONFIRMED"
        db.add(task)
        db.flush()
        claim = ApplicationClaim(user_id=uid, job_id=job.id, application_id=application.id, identity="apply:globex",
                                 token=uuid.uuid4().hex, client="worker", status="RECORDED",
                                 claimed_at=t0 + timedelta(hours=1, minutes=50),
                                 lease_expires_at=t0 + timedelta(hours=3), resolved_at=t0 + timedelta(hours=2))
        db.add(claim)
        db.flush()
        db.add(SubmissionIntent(user_id=uid, claim_id=claim.id, execution_task_id=task.id, job_id=job.id,
                                status="CONFIRMED", backend="playwright", route="greenhouse:globex",
                                domain="job-boards.greenhouse.io",
                                evidence={"responses": [{"method": "POST", "status": 200, "host": "boards-api.greenhouse.io"}],
                                          "form_gone": True, "confirmation_wording": True, "confirmation_path": False,
                                          "final_host": "job-boards.greenhouse.io"},
                                clicking_at=t0 + timedelta(hours=1, minutes=55), resolved_at=t0 + timedelta(hours=2)))
        msg = ApplicationEmailMessage(user_id=uid, gmail_message_id=uuid.uuid4().hex, sender=PRIVATE[2],
                                      subject=PRIVATE[3], relevant_text=PRIVATE[4],
                                      received_at=t0 + timedelta(hours=5),
                                      application_id=application.id if gmail == "MATCHED" else None,
                                      match_status=gmail, match_score=0.95 if gmail == "MATCHED" else 0.78,
                                      match_candidates=None if gmail == "MATCHED" else
                                      [{"application_id": application.id, "score": 0.78},
                                       {"application_id": application.id + 1000, "score": 0.76}])
        db.add(msg)
        db.flush()
        if gmail == "MATCHED":
            db.add(ApplicationCommunicationEvent(
                user_id=uid, application_id=application.id, message_id=msg.id, gmail_message_id=msg.gmail_message_id,
                event_type="APPLICATION_REJECTION", sender=PRIVATE[2], subject=PRIVATE[3], summary=PRIVATE[4],
                received_at=msg.received_at, confidence=0.93, classification_method="LLM", stage_decision="APPLIED",
                stage_from="applied", stage_to="rejected"))
        db.commit()
        return {"job": job.id, "application": application.id, "message": msg.id, "packet": packet.id}


def test_one_synthetic_application_is_correlated_end_to_end(signed_in):
    uid = make_user(SessionLocal)
    ids = seed_journey(uid)
    out = signed_in(uid).get(f"/api/orchestration/jobs/{ids['job']}/journey").json()
    assert [s["stage"] for s in out["stages"]] == list(journey.STAGES)
    by = {s["stage"]: s for s in out["stages"]}
    assert all(s["status"] == "DONE" for s in out["stages"]), {k: v["status"] for k, v in by.items()}
    assert by["discovery"]["board"] == "greenhouse"
    assert by["retrieval"]["retrieval_ms_max"] == 37 and by["retrieval"]["modes"] == {"on": 1}
    # Actual vs configured: the model that answered, from telemetry; the fallback is the gateway's own fact.
    calls = {c["stage"]: c for c in by["model"]["calls"]}
    assert calls["RANKER"]["actual_model"] == "ollama/gpt-oss:20b" and calls["RANKER"]["fallback_used"] is None
    assert calls["TAILOR"]["actual_model"] == "openai/gpt-4.1-mini" and calls["TAILOR"]["fallback_used"] is True
    assert calls["TAILOR"]["configured_model"] == "tailor-main"
    assert by["packet"]["digest_prefix"] == "d" * 12 and by["packet"]["approved_packet_id"] == ids["packet"]
    assert by["execution"]["mode"] == "GUARDED_AUTO" and by["execution"]["execution_stage"] == "CONFIRMED"
    assert by["confirmation"]["evidence"] == "verified_browser" and by["confirmation"]["form_gone"] is True
    assert by["confirmation"]["confirmation_id"] == "AC-12345"
    assert by["gmail_outcome"]["matched_message_ids"] == [ids["message"]]
    assert by["gmail_outcome"]["events"][0]["event_type"] == "APPLICATION_REJECTION"
    assert by["gmail_outcome"]["events"][0]["decided_by"] == "classifier"
    assert out["trace_ids"] == ["a" * 32]
    # The rejection is the recruiter's, beside the journey and attributed to nothing.
    assert out["recruiter_outcome"] == {"stage": "rejected", "status": "applied", "attributed_to_model": False}
    text = json.dumps(out)
    for private in PRIVATE:
        assert private not in text


def test_cost_counts_each_call_once_and_unknown_stays_unknown():
    uid = make_user(SessionLocal)
    ids = seed_journey(uid)
    with SessionLocal() as db:
        db.info["user_id"] = uid
        cost = journey.journey(db, ids["job"], uid)["cost"]
    # The ranker's $0.02 is on its LLM row and on the external row it points at: counted once. The embedding's
    # external row has no LLM row: counted. The tailoring call reported no cost: unpriced, not $0.
    assert cost["llm_cost"] == pytest.approx(0.02) and cost["external_cost"] == pytest.approx(0.0001)
    assert cost["cost"] == pytest.approx(0.0201)
    assert cost["external_rows_linked_to_llm"] == 1 and cost["unpriced"] == 1


def test_a_job_with_no_telemetry_is_unknown_not_zero():
    uid = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = uid
        job = Job(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Backend Engineer", company="Initech")
        db.add(job)
        db.commit()
        out = journey.journey(db, job.id, uid)
    assert out["unknown_stages"] == [s for s in journey.STAGES if s != "discovery"]
    assert out["cost"]["cost"] is None and out["cost"]["unpriced"] == 0
    assert out["recruiter_outcome"]["stage"] is None


def test_another_account_cannot_see_the_journey(signed_in):
    owner, other = make_user(SessionLocal), make_user(SessionLocal)
    ids = seed_journey(owner)
    assert signed_in(other).get(f"/api/orchestration/jobs/{ids['job']}/journey").status_code == 404
    with SessionLocal() as db:
        db.info["user_id"] = other
        assert journey.journey(db, ids["job"], other) is None
    # The other account's own (empty) feedback numbers are not the owner's.
    fb = signed_in(other).get("/api/monitoring/feedback").json()
    assert fb["samples"] == 0 and fb["recruiter_outcome"]["submitted"] == 0


def test_an_ambiguous_email_waits_for_review_and_is_never_this_applications_outcome():
    uid = make_user(SessionLocal)
    ids = seed_journey(uid, gmail="NEEDS_REVIEW")
    with SessionLocal() as db:
        db.info["user_id"] = uid
        gmail = {s["stage"]: s for s in journey.journey(db, ids["job"], uid)["stages"]}["gmail_outcome"]
        app_row = db.get(Application, ids["application"])
    assert gmail["status"] == "NEEDS_REVIEW" and gmail["review_message_ids"] == [ids["message"]]
    assert gmail["matched_message_ids"] == [] and gmail["events"] == []
    assert app_row.stage == "rejected"  # untouched by the ambiguous message (seeded by the tracker, not by email)


def test_a_persons_correction_overrides_the_classifier():
    uid = make_user(SessionLocal)
    ids = seed_journey(uid)
    with SessionLocal() as db:
        db.info["user_id"] = uid
        event = db.query(ApplicationCommunicationEvent).filter_by(application_id=ids["application"]).one()
        event.user_corrected = True
        event.corrections = [{"field": "event_type", "old": "APPLICATION_REJECTION", "new": "INTERVIEW_INVITATION",
                              "at": utcnow().isoformat()}]
        db.commit()
        gmail = {s["stage"]: s for s in journey.journey(db, ids["job"], uid)["stages"]}["gmail_outcome"]
        from app.services.agent_eval import feedback
        rates = feedback.report(db, uid)
    e = gmail["events"][0]
    assert (e["event_type"], e["decided_by"], e["classifier_event_type"]) == ("INTERVIEW_INVITATION", "person", "APPLICATION_REJECTION")
    assert rates["by_signal"]["inbox"] == {"corrected": 1, "samples": 1, "rate": 1.0}
    # The correction sets classification_method USER as the service does; the corrected result still counts.
    with SessionLocal() as db:
        db.info["user_id"] = uid
        db.query(ApplicationCommunicationEvent).filter_by(application_id=ids["application"]).update(
            {"classification_method": "USER"})
        db.commit()
        assert feedback.report(db, uid)["by_signal"]["inbox"]["rate"] == 1.0
    assert rates["recruiter_outcome"]["rejected"] == 1 and rates["recruiter_outcome"]["attributed_to_model"] is False


def test_the_trace_places_execution_claim_intent_and_email_metadata_only(signed_in):
    uid = make_user(SessionLocal)
    ids = seed_journey(uid, gmail="NEEDS_REVIEW")
    trace = signed_in(uid).get(f"/api/orchestration/jobs/{ids['job']}/trace").json()
    nodes = {e["node_id"] for e in trace["events"]}
    assert {"application_execution", "submission_confirmation", "inbox_review"} <= nodes
    text = json.dumps(trace)
    for private in PRIVATE:
        assert private not in text


def test_a_model_call_is_traced_to_its_job_so_its_gateway_row_is_too(monkeypatch):
    """LLMClient narrows the open trace to the call's job (same trace id): the external row of a gateway call made
    in a sweep-wide trace then carries the job, and the journey finds it."""
    seen = {}
    client = llm.LLMClient.__new__(llm.LLMClient)
    monkeypatch.setattr(llm.LLMClient, "_generate_prompt",
                        lambda self, prompt, call: seen.update(ctx=tracing.current()) or "ok")
    with tracing.trace("ranking sweep") as sweep:
        client.generate_prompt(object(), LLMCall(workflow_type="job", entity_id=42))
        assert tracing.current().job_id is None  # the sweep's own trace is untouched afterwards
    assert seen["ctx"].job_id == 42 and seen["ctx"].trace_id == sweep.trace_id
    client.generate_prompt(object(), LLMCall(workflow_type="application", entity_id=7))
    assert seen["ctx"].application_id == 7


def test_monitoring_reports_failover_mismatch_retrieval_and_unknown_cost():
    uid = make_user(SessionLocal)
    t = utcnow() - timedelta(minutes=30)
    rows = [llm_row(uid, 1, t, retrieval_ms=20, retrieval_mode="on"),
            llm_row(uid, 1, t, retrieval_ms=40, retrieval_mode="on", routing_cause="FAILOVER",
                    failed_over_from="ranker-main", model_group="ranker-backup"),
            llm_row(uid, 1, t, configured_model="AUTO_ROUTER", model_group="tier-1"),  # the router's choice: n/a
            llm_row(uid, 1, t, estimated_cost=None, cost_source="unknown", configured_model=None)]
    block = model_performance.block(rows, 3600)
    assert block["reliability"]["failover_rate"] == 0.25
    assert (block["reliability"]["model_mismatch_requests"], block["reliability"]["model_mismatch_samples"]) == (1, 2)
    assert (block["latency"]["retrieval_p50_ms"], block["latency"]["retrieval_p95_ms"]) == (20, 40)
    unpriced = monitoring.summary([llm_row(uid, 1, t, estimated_cost=None, cost_source="unknown")])
    assert unpriced["estimated_cost"] is None and unpriced["unpriced_requests"] == 1
    assert monitoring.summary([])["estimated_cost"] is None


def test_retrieval_timing_reaches_the_call_but_never_the_prompt():
    from app.services.context_builder import ContextStats, TaskContext

    ctx = TaskContext(task="ranking", candidate={}, resume={})
    ctx.stats = ContextStats.measure(100, 50)
    assert "retrieval_ms" not in ctx.call_stats() and "retrieval" not in ctx.to_dict()
    ctx.retrieval = {"retrieval_ms": 12, "retrieval_mode": "on"}
    assert ctx.call_stats()["retrieval_ms"] == 12 and "retrieval" not in ctx.to_dict()
    assert "retrieval" not in json.dumps(ctx.selected_payload(), default=str)


def test_the_journey_compares_configuration_as_monitoring_does():
    """The answering model group is what a configured deployment alias is compared with; the auto router's choice
    and an unknown side are not a mismatch (review: codex P1)."""
    uid = make_user(SessionLocal)
    t = utcnow() - timedelta(minutes=10)
    with SessionLocal() as db:
        db.info["user_id"] = uid
        job = Job(user_id=uid, source="test", source_ref=uuid.uuid4().hex, title="Backend Engineer", company="Acme")
        db.add(job)
        db.flush()
        db.add_all([llm_row(uid, job.id, t),  # ranker-main answered through ollama/gpt-oss:20b: as configured
                    llm_row(uid, job.id, t + timedelta(seconds=1), stage="TAILOR", configured_model="AUTO_ROUTER",
                            model_group="tier-1"),
                    llm_row(uid, job.id, t + timedelta(seconds=2), stage="REVIEWER", model_group="ranker-backup")])
        db.commit()
        calls = {c["stage"]: c for c in
                 {s["stage"]: s for s in journey.journey(db, job.id, uid)["stages"]}["model"]["calls"]}
    assert calls["RANKER"]["matches_configuration"] is True
    assert calls["TAILOR"]["matches_configuration"] is None
    assert calls["REVIEWER"]["matches_configuration"] is False


def test_a_submission_recorded_after_an_aborted_intent_is_the_confirmation():
    """An intent aborted before the click never hides the submission the person recorded afterwards (codex P1)."""
    uid = make_user(SessionLocal)
    ids = seed_journey(uid)
    with SessionLocal() as db:
        db.info["user_id"] = uid
        db.query(SubmissionIntent).filter_by(job_id=ids["job"]).update({"status": "ABORTED", "evidence": None})
        db.query(Application).filter_by(id=ids["application"]).update({"submitted_via": "apply_kit"})
        db.commit()
        stage = {s["stage"]: s for s in journey.journey(db, ids["job"], uid)["stages"]}["confirmation"]
    assert (stage["status"], stage["evidence"], stage["submitted_via"]) == ("DONE", "reported", "apply_kit")
    assert stage["confirmation_id"] == "AC-12345"
