"""Agent graph runtime (app.agents): flags, thread identity, tenant isolation, branches, interrupts, effects, stop.

These run on any database with an in-memory checkpointer; tests/test_agents_postgres.py covers the PostgreSQL
checkpointer, process restarts and the API end to end.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents import checkpoint, effects, runtime, threads
from app.db import SessionLocal
from app.main import app
from app.models import AgentThread
from app.services.lifecycle import STOPPED_BY_USER
from app.services.scheduled_jobs import JobContext, agent_graphs, cleanup
from app.settings import get_settings
from tests import agents_support as support
from tests.conftest import make_user

client = TestClient(app)


class Crash(BaseException):
    """What a killed process looks like to the code after it: nothing past this point runs."""


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", True)


@pytest.fixture
def two_accounts():
    return make_user(SessionLocal), make_user(SessionLocal)


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def ctx() -> JobContext:
    return JobContext(sessions=SessionLocal, trigger="schedule", now=now(), worker_id="test", interval_minutes=5)


# ───────────── off by default ─────────────

def test_everything_is_off_by_default():
    settings = get_settings()
    assert settings.agent_graphs_enabled is False and settings.agent_langsmith_export_enabled is False
    assert settings.agent_recovery_interval_minutes == 0
    assert client.get("/api/agents/runs").status_code == 404
    assert client.post("/api/agents/pilot/runs", json={"kind": "auto"}).status_code == 404
    assert client.post("/api/agents/runs/1/resume", json={"decision": "approve"}).status_code == 404
    assert asyncio.run(agent_graphs(ctx())) == {"skipped": "Agent graphs are off (AGENT_GRAPHS_ENABLED)"}
    assert "agent_checkpoints" not in cleanup(ctx())
    assert runtime.prune(SessionLocal) == {"skipped": "Agent graphs are off"}


def test_graphs_refuse_a_database_without_durable_checkpoints(enabled):
    if SessionLocal().get_bind().dialect.name == "postgresql":
        pytest.skip("SQLite only")
    assert client.get("/api/agents/runs").status_code == 503


# ───────────── identity and ownership ─────────────

def test_thread_ids_are_built_on_the_server_and_name_their_owner():
    subject = threads.subject_for(7, None)
    thread_id = threads.thread_id_for("pilot", 7, subject, "v1")
    assert thread_id == f"pilot:u7:{subject}:v1" and threads.owner_in(thread_id) == 7
    assert threads.subject_for(7, "same-key-1") == threads.subject_for(7, "same-key-1")
    assert threads.subject_for(7, "same-key-1") != threads.subject_for(8, "same-key-1")
    for bad in (("pilot", 7, "x:u8", "v1"), ("Pilot", 7, subject, "v1"), ("pilot", 7, subject, "1")):
        with pytest.raises(ValueError):
            threads.thread_id_for(*bad)
    assert threads.owner_in("pilot:u7:not-hex:v1") is None


def test_starting_twice_with_the_same_key_returns_the_same_run(two_accounts):
    a, b = two_accounts
    first = support.start(SessionLocal, a, key="idempotent-key-1")
    assert support.start(SessionLocal, a, key="idempotent-key-1") == first
    assert support.start(SessionLocal, b, key="idempotent-key-1") != first  # the key is per account
    assert support.start(SessionLocal, a) != first


def test_input_is_validated_and_never_carries_extra_fields(two_accounts):
    a, _ = two_accounts
    with pytest.raises(ValidationError):
        support.start(SessionLocal, a, {"kind": "auto", "items": 3, "api_key": support.SECRETS["openai"]})
    with pytest.raises(ValidationError):
        support.start(SessionLocal, a, {"kind": "deploy"})
    with pytest.raises(ValidationError):
        support.start(SessionLocal, a, {"items": 999})


def test_one_account_cannot_execute_list_resume_or_stop_anothers_run(two_accounts):
    a, b = two_accounts
    saver = support.memory_saver()
    mine = support.start(SessionLocal, a, {"kind": "review", "items": 2})
    assert runtime.execute(SessionLocal, support.claim(SessionLocal, a, mine), saver=saver) == "interrupted"

    assert support.claim(SessionLocal, b, mine, ("QUEUED", "INTERRUPTED")) is None  # claim is per owner
    with support.as_user(SessionLocal, b) as db:
        assert mine not in [t.id for t in threads.listed(db)]
        with pytest.raises(threads.ThreadNotFound):
            threads.owned(db, mine)
        with pytest.raises(threads.ThreadNotFound):
            runtime.resume(db, mine, {"decision": "approve"}, "b")
        with pytest.raises(threads.ThreadNotFound):
            runtime.stop(db, mine)
    # A run handle naming another account is refused before the checkpointer is touched.
    assert runtime.execute(SessionLocal, runtime.RunHandle(mine, b, "forged"), saver=saver) == "refused"
    assert runtime.execute(SessionLocal, runtime.RunHandle(mine, a, "stale-token"), saver=saver) == "lost"
    assert support.thread(SessionLocal, mine).status == "INTERRUPTED"
    with support.as_user(SessionLocal, a) as db:
        assert [t.id for t in threads.listed(db)].count(mine) == 1


def test_a_thread_id_naming_another_owner_is_refused(two_accounts):
    a, b = two_accounts
    mine = support.start(SessionLocal, a)
    with SessionLocal() as db:  # what a corrupted or hand-edited row would look like
        row = db.get(AgentThread, mine)
        row.thread_id = threads.thread_id_for("pilot", b, row.subject, "v1")
        db.commit()
    with support.as_user(SessionLocal, a) as db, pytest.raises(threads.ThreadOwnershipError):
        threads.owned(db, mine)


def test_the_api_shows_metadata_only(two_accounts):
    a, _ = two_accounts
    mine = support.start(SessionLocal, a)
    shown = runtime.public(support.thread(SessionLocal, mine))
    assert set(shown) == {"id", "graph", "graph_version", "status", "interrupt", "result", "attempts", "last_error",
                          "created_at", "updated_at", "started_at", "completed_at"}


# ───────────── branches, interrupts, effects ─────────────

def test_auto_branch_completes_with_exactly_one_effect(two_accounts):
    a, _ = two_accounts
    pk = support.start(SessionLocal, a, {"kind": "auto", "items": 4})
    assert runtime.execute(SessionLocal, support.claim(SessionLocal, a, pk), saver=support.memory_saver()) == "completed"
    row = support.thread(SessionLocal, pk)
    assert row.status == "COMPLETED" and row.result == {"kind": "auto", "items": 4, "total": 10, "outcome": "completed",
                                                        "step": 4}
    assert row.lock_token is None and row.input is None and row.attempt_count == 1
    assert [e.effect_key for e in support.effects(SessionLocal, pk)] == ["pilot.completed"]


def test_review_branch_waits_for_a_person_then_resumes_from_its_checkpoint(two_accounts):
    a, _ = two_accounts
    saver = support.memory_saver()
    pk = support.start(SessionLocal, a, {"kind": "review", "items": 3})
    assert runtime.execute(SessionLocal, support.claim(SessionLocal, a, pk), saver=saver) == "interrupted"
    row = support.thread(SessionLocal, pk)
    assert row.status == "INTERRUPTED" and row.interrupt == {"kind": "approval", "items": 3, "total": 6}
    assert support.effects(SessionLocal, pk) == []

    run, command = support.resume(SessionLocal, a, pk, "approve")
    assert support.thread(SessionLocal, pk).interrupt is None
    assert runtime.execute(SessionLocal, run, command, saver=saver) == "completed"
    row = support.thread(SessionLocal, pk)
    assert row.result["decision"] == "approve" and row.result["outcome"] == "completed" and row.attempt_count == 2
    assert len(support.effects(SessionLocal, pk)) == 1


def test_a_rejected_review_makes_no_effect(two_accounts):
    a, _ = two_accounts
    saver = support.memory_saver()
    pk = support.start(SessionLocal, a, {"kind": "review", "items": 1})
    runtime.execute(SessionLocal, support.claim(SessionLocal, a, pk), saver=saver)
    run, command = support.resume(SessionLocal, a, pk, "reject")
    assert runtime.execute(SessionLocal, run, command, saver=saver) == "completed"
    assert support.thread(SessionLocal, pk).result["outcome"] == "rejected"
    assert support.effects(SessionLocal, pk) == []


def test_a_resume_takes_only_a_valid_decision_for_a_waiting_run(two_accounts):
    a, _ = two_accounts
    saver = support.memory_saver()
    pk = support.start(SessionLocal, a, {"kind": "review", "items": 1})
    with support.as_user(SessionLocal, a) as db, pytest.raises(runtime.ThreadBusy):
        runtime.resume(db, pk, {"decision": "approve"}, "test")  # still QUEUED
    runtime.execute(SessionLocal, support.claim(SessionLocal, a, pk), saver=saver)
    with support.as_user(SessionLocal, a) as db:
        for bad in ({"decision": "ship it"}, {"decision": "approve", "token": "x"}, {}):
            with pytest.raises(ValidationError):
                runtime.resume(db, pk, bad, "test")
    assert support.thread(SessionLocal, pk).status == "INTERRUPTED"
    support.resume(SessionLocal, a, pk, "approve")
    with pytest.raises(runtime.ThreadBusy):  # the first resume holds it
        support.resume(SessionLocal, a, pk, "approve")


def test_a_node_replayed_after_a_crash_finds_its_effect(two_accounts):
    """The process dies after the effect committed and before the checkpoint: the replay must not repeat it."""
    a, _ = two_accounts
    saver = support.memory_saver()
    pk = support.start(SessionLocal, a, {"kind": "auto", "items": 2})

    def die(point: str) -> None:
        if point == "finalize:after_effect":
            raise Crash
    with pytest.raises(Crash):
        runtime.execute(SessionLocal, support.claim(SessionLocal, a, pk), saver=saver, hold=die)
    assert support.thread(SessionLocal, pk).status == "RUNNING" and len(support.effects(SessionLocal, pk)) == 1

    with SessionLocal() as db:  # the dead worker's lease runs out
        db.get(AgentThread, pk).lock_expires_at = now() - timedelta(seconds=1)
        db.commit()
    runs = [r for r in runtime.claim_stale(SessionLocal, "recovery", 50) if r.thread_pk == pk]
    assert len(runs) == 1
    assert runtime.execute(SessionLocal, runs[0], saver=saver) == "completed"
    assert len(support.effects(SessionLocal, pk)) == 1
    assert support.thread(SessionLocal, pk).attempt_count == 2


def test_recovery_gives_up_after_the_attempt_limit(two_accounts, monkeypatch):
    a, _ = two_accounts
    monkeypatch.setattr(get_settings(), "agent_max_attempts", 1)
    pk = support.start(SessionLocal, a)
    support.claim(SessionLocal, a, pk)  # attempt 1, then the worker dies
    with SessionLocal() as db:
        db.get(AgentThread, pk).lock_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert pk not in [r.thread_pk for r in runtime.claim_stale(SessionLocal, "recovery", 50)]
    assert support.thread(SessionLocal, pk).status == "FAILED"


# ───────────── stopping ─────────────

def test_a_stopped_run_writes_nothing_more_and_is_never_recovered(two_accounts):
    a, _ = two_accounts
    pk = support.start(SessionLocal, a, {"kind": "auto", "items": 2})

    def stop_midway(point: str) -> None:
        if point == "compute":
            with support.as_user(SessionLocal, a) as db:
                runtime.stop(db, pk)
    run = support.claim(SessionLocal, a, pk)
    assert runtime.execute(SessionLocal, run, saver=support.memory_saver(), hold=stop_midway) == "lost"
    row = support.thread(SessionLocal, pk)
    assert row.status == "STOPPED" and row.last_error == STOPPED_BY_USER
    assert support.effects(SessionLocal, pk) == []
    assert pk not in [r.thread_pk for r in runtime.claim_stale(SessionLocal, "recovery", 50)]
    with support.as_user(SessionLocal, a) as db, pytest.raises(runtime.ThreadBusy):
        runtime.stop(db, pk)


def test_effects_need_the_lease(two_accounts):
    a, _ = two_accounts
    pk = support.start(SessionLocal, a)
    run = support.claim(SessionLocal, a, pk)
    with SessionLocal() as db:
        record, created = effects.once(db, run, "test.effect", {"n": 1})
        again, created_again = effects.once(db, run, "test.effect", {"n": 2})
        assert created and not created_again and again.id == record.id and again.summary == {"n": 1}
    with SessionLocal() as db, pytest.raises(effects.LeaseLost):
        effects.once(db, runtime.RunHandle(pk, a, "someone-else"), "test.other")
    with SessionLocal() as db, pytest.raises(ValueError):
        effects.once(db, run, "test.secret", {"password": support.SECRETS["password"]})


# ───────────── checkpoint guard ─────────────

def test_checkpoints_refuse_secrets_and_oversized_state():
    serde = checkpoint.GuardedSerializer()
    assert serde.loads_typed(serde.dumps_typed({"kind": "auto", "items": 3, "total": 6})) == {"kind": "auto",
                                                                                            "items": 3, "total": 6}
    for name in support.SHAPED:
        with pytest.raises(checkpoint.CheckpointRejected):
            serde.dumps_typed({"note": f"value {support.SECRETS[name]}"})
    for field in ("password", "otp", "cookie", "authorization", "access_token", "storage_state", "api_key"):
        with pytest.raises(checkpoint.CheckpointRejected):
            serde.dumps_typed({"nested": [{field: "anything"}]})
    with pytest.raises(checkpoint.CheckpointRejected):
        serde.dumps_typed({"blob": "x" * (get_settings().agent_checkpoint_max_bytes + 1)})
