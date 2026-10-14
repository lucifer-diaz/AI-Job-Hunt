"""Agent graphs on PostgreSQL: durable checkpoints across a killed worker, tenant isolation, retention, the API.

Needs TEST_DATABASE_URL (skipped otherwise). The restart tests start a real worker process, kill it mid-run, and
continue the run from this process with a fresh connection, as the recovery pass would after a crash.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.agents import checkpoint, runtime, threads
from app.auth import CurrentUser
from app.db import AppSession, SessionLocal
from app.models import AgentThread
from app.settings import get_settings
from tests import agents_support as support
from tests.conftest import WORKFLOW_TEST_SCHEMA, make_user

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sessions(pg_sessions, pg_engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", True)
    checkpoint.forget_setup()  # other tests may have dropped and re-created the schema
    return sessionmaker(bind=pg_engine, class_=AppSession, autoflush=False)


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def checkpoints_of(engine, thread_id: str) -> int:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT COUNT(*) FROM checkpoints WHERE thread_id = :t"), {"t": thread_id}) or 0


def worker(pg_url: str, thread_pk: int, user_id: int, mode: str, marker: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "tests.agents_proc", pg_url, WORKFLOW_TEST_SCHEMA, str(thread_pk),
                             str(user_id), mode, str(marker)], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})


def wait_for(marker: Path, process: subprocess.Popen, seconds: float = 60) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text():
            return marker.read_text()
        if process.poll() is not None and not marker.exists():
            raise AssertionError(f"worker exited with {process.returncode} before reaching the marker")
        time.sleep(0.1)
    process.kill()
    raise AssertionError("worker did not reach the marker in time")


def expire_lease(sessions, thread_pk: int) -> None:
    with sessions() as db:
        db.get(AgentThread, thread_pk).lock_expires_at = now() - timedelta(seconds=1)
        db.commit()


def recover(sessions, thread_pk: int) -> str:
    runs = [r for r in runtime.claim_stale(sessions, "recovery", 50) if r.thread_pk == thread_pk]
    assert len(runs) == 1
    return runtime.execute(sessions, runs[0])


# ───────────── restart ─────────────

def test_a_killed_worker_resumes_from_its_postgres_checkpoint(sessions, pg_engine, pg_url, tmp_path):
    user = make_user(sessions)
    pk = support.start(sessions, user, {"kind": "auto", "items": 5})
    marker = tmp_path / "marker"
    process = worker(pg_url, pk, user, "hang_at_compute", marker)
    assert wait_for(marker, process) == "compute"
    thread_id = support.thread(sessions, pk).thread_id
    assert checkpoints_of(pg_engine, thread_id) >= 1  # the steps before `compute` are durable
    os.kill(process.pid, signal.SIGKILL)
    process.wait(30)
    row = support.thread(sessions, pk)
    assert row.status == "RUNNING" and row.lock_token is not None  # what a dead worker leaves behind
    assert support.claim(sessions, user, pk, ("QUEUED", "RUNNING")) is None  # its lease still protects it

    expire_lease(sessions, pk)
    assert recover(sessions, pk) == "completed"
    row = support.thread(sessions, pk)
    assert row.status == "COMPLETED" and row.result["total"] == 15 and row.attempt_count == 2
    assert len(support.effects(sessions, pk)) == 1


def test_a_worker_killed_after_its_effect_does_not_repeat_it(sessions, pg_url, tmp_path):
    user = make_user(sessions)
    pk = support.start(sessions, user, {"kind": "auto", "items": 2})
    marker = tmp_path / "marker"
    process = worker(pg_url, pk, user, "die_after_effect", marker)
    assert wait_for(marker, process) == "effect"
    process.wait(30)
    assert len(support.effects(sessions, pk)) == 1 and support.thread(sessions, pk).status == "RUNNING"

    expire_lease(sessions, pk)
    assert recover(sessions, pk) == "completed"  # `finalize` runs again: its checkpoint was never written
    assert len(support.effects(sessions, pk)) == 1
    assert runtime.claim_stale(sessions, "recovery", 50) == []  # and a second pass has nothing to repeat


def test_an_interrupt_survives_a_restart_and_resumes_without_reading_a_secret(sessions, pg_engine, pg_url, tmp_path,
                                                                             monkeypatch):
    from app.services.user_settings import set_user_keys

    user = make_user(sessions)
    with support.as_user(sessions, user) as db:  # the account has real-looking saved credentials
        set_user_keys(db, {"openai": support.SECRETS["openai"], "anthropic": support.SECRETS["anthropic"]}, user)
        db.commit()
    pk = support.start(sessions, user, {"kind": "review", "items": 3})
    marker = tmp_path / "marker"
    process = worker(pg_url, pk, user, "until_interrupt", marker)
    assert wait_for(marker, process) == "interrupted"
    process.wait(30)

    import app.security

    def no_secrets(*args, **kwargs):
        raise AssertionError("resuming a graph must not decrypt any credential")
    monkeypatch.setattr(app.security, "_fernet", no_secrets)
    run, command = support.resume(sessions, user, pk, "approve")  # a different process from the one that paused
    assert runtime.execute(sessions, run, command) == "completed"
    assert support.thread(sessions, pk).result["decision"] == "approve"
    assert len(support.effects(sessions, pk)) == 1

    with pg_engine.connect() as connection:
        stored = b"".join(bytes(r[0] or b"") for r in connection.execute(text(
            "SELECT blob FROM checkpoint_blobs WHERE thread_id = :t UNION ALL "
            "SELECT blob FROM checkpoint_writes WHERE thread_id = :t"), {"t": support.thread(sessions, pk).thread_id}))
        meta = " ".join(str(r[0]) + str(r[1]) for r in connection.execute(text(
            "SELECT checkpoint, metadata FROM checkpoints WHERE thread_id = :t"),
            {"t": support.thread(sessions, pk).thread_id}))
    for value in support.SECRETS.values():
        assert value.encode() not in stored and value not in meta


# ───────────── isolation and retention ─────────────

def test_tenant_isolation_holds_on_postgres(sessions):
    a, b = make_user(sessions), make_user(sessions)
    pk = support.start(sessions, a, {"kind": "review", "items": 1})
    assert runtime.execute(sessions, support.claim(sessions, a, pk)) == "interrupted"
    with support.as_user(sessions, b) as db:
        assert threads.listed(db) == []
        with pytest.raises(threads.ThreadNotFound):
            runtime.resume(db, pk, {"decision": "approve"}, "b")
    assert runtime.execute(sessions, runtime.RunHandle(pk, b, "forged")) == "refused"
    assert support.claim(sessions, b, pk, ("INTERRUPTED",)) is None
    assert support.thread(sessions, pk).status == "INTERRUPTED"


def test_retention_prunes_finished_expired_and_orphaned_checkpoints(sessions, pg_engine, monkeypatch):
    settings = get_settings()
    user = make_user(sessions)
    done = support.start(sessions, user)
    runtime.execute(sessions, support.claim(sessions, user, done))
    waiting = support.start(sessions, user, {"kind": "review", "items": 1})
    runtime.execute(sessions, support.claim(sessions, user, waiting))
    gone_user = make_user(sessions)
    gone = support.start(sessions, gone_user)
    runtime.execute(sessions, support.claim(sessions, gone_user, gone))
    ids = {pk: support.thread(sessions, pk).thread_id for pk in (done, waiting, gone)}
    assert all(checkpoints_of(pg_engine, t) for t in ids.values())
    with sessions() as db:  # the account is deleted: its rows cascade, its checkpoints are orphans
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": gone_user})
        db.commit()

    monkeypatch.setattr(settings, "agent_checkpoint_retention_days", 0)
    monkeypatch.setattr(settings, "agent_interrupt_expiry_days", 1)
    later = now() + timedelta(days=2)
    result = runtime.prune(sessions, later)
    assert result["expired"] >= 1 and result["pruned"] >= 1 and result["orphans"] >= 1, result
    assert support.thread(sessions, waiting).status == "STOPPED"
    assert runtime.prune(sessions, later + timedelta(minutes=1))["pruned"] >= 1  # the expired one: next pass
    assert not any(checkpoints_of(pg_engine, t) for t in ids.values())
    assert support.thread(sessions, done).checkpoint_pruned_at is not None


# ───────────── the API, end to end ─────────────

@pytest.fixture
def api(monkeypatch):
    if SessionLocal().get_bind().dialect.name != "postgresql":
        pytest.skip("the API test needs the application database on PostgreSQL")
    monkeypatch.setattr(get_settings(), "agent_graphs_enabled", True)
    from app import auth
    from app.main import app

    acting: dict[str, int] = {}
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(acting["id"], f"u{acting['id']}@x.test",
                                                                          False))
    return TestClient(app), acting


def test_the_api_runs_interrupts_and_resumes_the_pilot_for_its_owner_only(api):
    client, acting = api
    a, b = make_user(SessionLocal), make_user(SessionLocal)
    acting["id"] = a
    started = client.post("/api/agents/pilot/runs", json={"kind": "review", "items": 2,
                                                           "idempotency_key": "api-test-key-1"})
    assert started.status_code == 202
    run_id = started.json()["id"]
    shown = client.get(f"/api/agents/runs/{run_id}").json()
    assert shown["status"] == "INTERRUPTED" and shown["interrupt"] == {"kind": "approval", "items": 2, "total": 3}
    assert "thread_id" not in shown and "input" not in shown
    again = client.post("/api/agents/pilot/runs", json={"kind": "review", "items": 2,
                                                        "idempotency_key": "api-test-key-1"})
    assert again.json()["id"] == run_id
    assert client.post("/api/agents/pilot/runs", json={"kind": "auto", "token": "x"}).status_code == 422

    acting["id"] = b
    assert client.get(f"/api/agents/runs/{run_id}").status_code == 404
    assert client.post(f"/api/agents/runs/{run_id}/resume", json={"decision": "approve"}).status_code == 404
    assert client.post(f"/api/agents/runs/{run_id}/stop").status_code == 404
    assert run_id not in [r["id"] for r in client.get("/api/agents/runs").json()["runs"]]

    acting["id"] = a
    assert client.post(f"/api/agents/runs/{run_id}/resume", json={"decision": "maybe"}).status_code == 422
    assert client.post(f"/api/agents/runs/{run_id}/resume", json={"decision": "approve"}).status_code == 202
    done = client.get(f"/api/agents/runs/{run_id}").json()
    assert done["status"] == "COMPLETED" and done["result"]["outcome"] == "completed"
    assert client.post(f"/api/agents/runs/{run_id}/resume", json={"decision": "approve"}).status_code == 409
    assert checkpoint.durable(SessionLocal().get_bind())
