"""The retrieval indexer in Running workers (kind "indexing"): rows without a job, Kill (stop, never requeue, lease
guarded), Retry, Run now (first in line, past the provider cooldown), pause (the queue is held, never dropped), the
account's attempt limit, and the totals. Runs on SQLite and, with TEST_DATABASE_URL, on PostgreSQL."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Evidence, RagIndexEntry, RagIndexVersion
from app.services import backoff, rag_index, rag_retrieval, vector_store, worker_settings, workers
from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.lifecycle import STOPPED_BY_USER
from app.settings import get_settings
from tests import rag_support as rs
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "rag_vector_enabled", True)
    monkeypatch.setattr(get_settings(), "rag_backfill_min_coverage", 1.0)
    rag_retrieval.clear_cache()
    backoff.reset_cache()
    rs.use_concepts(monkeypatch)


@pytest.fixture
def data():
    return rs.load(SessionLocal, make_user(SessionLocal))


@pytest.fixture
def client(data, monkeypatch):
    monkeypatch.setattr(auth, "authenticate",
                        lambda request: CurrentUser(data.user_id, f"{data.user_id}@jobhunt.test", False))
    return TestClient(app)


class Recorder(rs.ConceptBackend):
    """Records whether each embedding call ran with the provider cooldown bypassed, and can fail."""

    def __init__(self, fail: str | None = None):
        super().__init__()
        self.bypassed: list[bool] = []
        self.calls: list[int] = []
        self.fail = fail

    def embed(self, model, texts, operation):
        self.bypassed.append(backoff.bypassing())
        self.calls.append(len(texts))
        if self.fail:
            raise EmbeddingError(self.fail, "provider says no")
        return super().embed(model, texts, operation)


def service(data, backend=None) -> EmbeddingService:
    return EmbeddingService(rs.CONCEPT_MODEL, backend or rs.ConceptBackend(), data.user_id)


def run(data, backend=None):
    return rag_index.run_account(SessionLocal, data.user_id, service=service(data, backend))


def queued(data) -> int:
    """Sweep the account's entries (PENDING) without embedding anything; returns the version id."""
    with rs.session_for(SessionLocal, data.user_id) as db:
        version = rag_index.ensure_version(db, data.user_id, rs.CONCEPT_MODEL)
        rag_index.sweep(db, data.user_id, version)
        db.commit()
        version_id = version.id
    vector_store.setup(SessionLocal().get_bind())
    return version_id


def entry(data, entry_id: int) -> RagIndexEntry:
    with rs.session_for(SessionLocal, data.user_id) as db:
        return db.get(RagIndexEntry, entry_id)


def entries(data) -> list[RagIndexEntry]:
    with rs.session_for(SessionLocal, data.user_id) as db:
        return list(db.scalars(select(RagIndexEntry).where(RagIndexEntry.user_id == data.user_id)
                               .order_by(RagIndexEntry.id)).all())


def claim(data, version_id: int, limit: int = 100) -> rag_index.Claimed | None:
    with rs.session_for(SessionLocal, data.user_id) as db:
        return rag_index.claim(db, data.user_id, version_id, limit, at=rag_index.now())


def pause(data, auto: bool = False, **values) -> None:
    with rs.session_for(SessionLocal, data.user_id) as db:
        worker_settings.update(db, data.user_id, {"indexing": {"auto": auto, **values}})


# ───────────── listing ─────────────

def test_the_indexing_tab_lists_entries_without_a_job(data, client):
    queued(data)
    other = rs.load(SessionLocal, make_user(SessionLocal))
    queued(other)
    summary = client.get("/api/workers").json()
    tab = next(w for w in summary["workers"] if w["kind"] == "indexing")
    assert tab["label"] == "Indexing" and tab["node_id"] == "retrieval_index" and not tab["paused"]
    assert tab["counts"] == {"running": 0, "queued": len(data.facts), "retrying": 0, "active": len(data.facts)}
    assert summary["active"] >= len(data.facts)  # counted in the totals like any queue
    listed = client.get("/api/workers/indexing").json()
    assert listed["total"] == len(data.facts)  # only this account's entries
    first = listed["rows"][0]
    assert first["job_id"] is None and first["title"].startswith(("Evidence #", "Saved answer #"))
    assert first["company"].startswith("index v") and first["subject"]["type"] in ("evidence", "knowledge_answer")
    assert first["model"]["model"] == rs.CONCEPT_MODEL.model and first["model"]["source"] == "next"
    assert first["status"] == "QUEUED" and not first["can_kill"] and first["can_run_now"]
    # metadata only: never the fact's text
    claims = {e.claim for e in SessionLocal().scalars(select(Evidence).where(Evidence.user_id == data.user_id))}
    assert not any(c and c in str(listed) for c in claims)
    fact = data.facts["churn"]
    assert [r["subject"]["id"] for r in client.get(f"/api/workers/indexing?q=%23{fact}").json()["rows"]] == [fact]


def test_finished_entries_show_as_done_and_retired_indexes_are_left_out(data, client):
    run(data)
    rows = client.get("/api/workers/indexing").json()["rows"]
    assert rows and {r["status"] for r in rows} == {"COMPLETED"} and all(r["model"]["source"] == "used" for r in rows)
    assert client.get("/api/workers/indexing/activity").json()["done"] == len(rows)
    with rs.session_for(SessionLocal, data.user_id) as db:
        db.execute(update(RagIndexVersion).where(RagIndexVersion.user_id == data.user_id)
                   .values(status="RETIRED", retired_at=rag_index.now()))
        db.commit()
    assert client.get("/api/workers/indexing").json()["rows"] == []


# ───────────── Kill ─────────────

def test_kill_stops_a_running_entry_and_its_vector_is_never_stored(data, client):
    version_id = queued(data)
    claimed = claim(data, version_id)
    target = claimed.entries[0][0]
    assert client.get("/api/workers/indexing").json()["status_counts"]["running"] == len(claimed.entries)
    out = client.post(f"/api/workers/indexing/tasks/{target}/kill").json()
    assert out == {"kind": "indexing", "stopped": 1, "running": 1, "cooldowns_cleared": []}
    stopped = entry(data, target)
    assert (stopped.status, stopped.last_error, stopped.lock_token) == ("FAILED", STOPPED_BY_USER, None)
    done = rag_index.process(SessionLocal, data.user_id, service(data), version_id, 64, claimed)
    assert done["lost_lease"] == 1 and done["embedded"] == len(claimed.entries) - 1  # the rest of the batch runs
    assert entry(data, target).status == "FAILED" and entry(data, target).content_hash is None
    row = next(r for r in client.get("/api/workers/indexing").json()["rows"] if r["task_id"] == target)
    assert row["status"] == "STOPPED" and row["can_run_now"] and not row["can_kill"]


def test_a_stopped_entry_stays_stopped_until_a_person_retries_it(data, client):
    version_id = queued(data)
    target = claim(data, version_id, limit=1).entries[0]
    client.post(f"/api/workers/indexing/tasks/{target[0]}/kill")
    run(data)  # the next passes: sweep and claim leave it alone
    run(data)
    assert entry(data, target[0]).last_error == STOPPED_BY_USER
    if target[1] == "evidence":  # an edit does not revive it either
        with rs.session_for(SessionLocal, data.user_id) as db:
            db.get(Evidence, target[2]).claim += " (edited)"
            db.commit()
        assert entry(data, target[0]).status == "FAILED"
    out = client.post("/api/workers/indexing/retry", json={"task_ids": [target[0]]}).json()
    assert out["retried"] == [{"task_id": target[0], "new_task_id": target[0], "job_id": None}]
    assert entry(data, target[0]).status == "PENDING"
    run(data)
    assert entry(data, target[0]).status == "ACTIVE"


def test_kill_all_stops_retrying_entries_but_not_ones_waiting_their_first_turn(data, client):
    version_id = queued(data)
    claimed = claim(data, version_id, limit=2)
    rag_index._fail(SessionLocal, data.user_id, claimed.token, [e[0] for e in claimed.entries],
                    EmbeddingError("REJECTED", "no"))
    retrying = {e[0] for e in claimed.entries}
    assert {entry(data, i).status for i in retrying} == {"PENDING"}
    out = client.post("/api/workers/indexing/kill-all").json()
    assert out["stopped"] == 2 and out["running"] == 0
    after = {e.id: e for e in entries(data)}
    assert all(after[i].last_error == STOPPED_BY_USER for i in retrying)
    assert all(e.status == "PENDING" and e.last_error is None for i, e in after.items() if i not in retrying)
    everything = client.post("/api/workers/kill-all").json()
    assert "indexing" in everything["workers"]


# ───────────── Run now and pause ─────────────

def test_run_now_goes_first_in_its_own_batch_past_the_provider_cooldown(data, client):
    version_id = queued(data)
    last = entries(data)[-1].id
    row = client.post(f"/api/workers/indexing/tasks/{last}/run-now").json()
    assert row["run_now"] and row["status"] == "QUEUED"
    claimed = claim(data, version_id)
    assert claimed.run_now and [e[0] for e in claimed.entries] == [last]
    assert entry(data, last).run_now_at is None  # cleared on claim
    backend = Recorder()
    rag_index.process(SessionLocal, data.user_id, service(data, backend), version_id, 64, claimed)
    assert backend.bypassed == [True]
    rest = claim(data, version_id)
    assert not rest.run_now and last not in {e[0] for e in rest.entries}
    rag_index.process(SessionLocal, data.user_id, service(data, backend), version_id, 64, rest)
    assert backend.bypassed == [True, False]


def test_run_now_queues_a_failed_entry_again(data, client):
    pause(data, auto=True, max_attempts=1)
    run(data, Recorder(fail="REJECTED"))
    failed = [e for e in entries(data) if e.status == "FAILED"]
    assert failed and all(e.last_error.startswith("REJECTED") for e in failed)  # the account's own attempt limit
    row = client.post(f"/api/workers/indexing/tasks/{failed[0].id}/run-now").json()
    assert row["status"] == "QUEUED" and row["run_now"] and row["attempt"] == 0


def test_a_paused_account_keeps_its_queue_and_runs_only_what_a_person_asks_for(data, client):
    pause(data)
    summary = run(data)
    assert summary["embedded"] == 0 and len(entries(data)) == len(data.facts)  # swept, held, nothing dropped
    tab = next(w for w in client.get("/api/workers").json()["workers"] if w["kind"] == "indexing")
    assert tab["paused"] and tab["counts"]["queued"] == len(data.facts)
    one = entries(data)[0].id
    client.post(f"/api/workers/indexing/tasks/{one}/run-now")
    assert run(data)["embedded"] == 1 and entry(data, one).status == "ACTIVE"
    pause(data, auto=True)
    assert run(data)["embedded"] == len(data.facts) - 1


def test_a_failure_backs_off_with_the_accounts_own_wait(data):
    pause(data, auto=True, retry_seconds=600)
    before = rag_index.now()
    run(data, Recorder(fail="REJECTED"))
    waiting = entries(data)
    assert waiting and all(e.status == "PENDING" and e.attempt_count == 1 for e in waiting)
    assert all(e.next_attempt_at >= before + timedelta(seconds=590) for e in waiting)
    row = workers.rows(SessionLocal(), data.user_id, "indexing")["rows"][0]
    assert row["status"] == "RETRYING" and row["can_kill"]


def test_an_abandoned_entry_on_its_last_attempt_is_failed_not_retried(data):
    version_id = queued(data)
    pause(data, auto=True, max_attempts=1)
    claimed = claim(data, version_id, limit=1)
    with rs.session_for(SessionLocal, data.user_id) as db:  # the run died; its lease ran out
        db.execute(update(RagIndexEntry).where(RagIndexEntry.lock_token == claimed.token)
                   .values(lock_expires_at=rag_index.now() - timedelta(seconds=1)))
        db.commit()
    again = claim(data, version_id)
    assert claimed.entries[0][0] not in {e[0] for e in again.entries}
    assert entry(data, claimed.entries[0][0]).last_error.startswith("LEASE_EXPIRED")


# ───────────── the scheduler pass ─────────────

def test_the_scheduler_pass_heartbeat_stops_waiting_for_a_killed_batch(data):
    version_id = queued(data)
    claimed = claim(data, version_id, limit=2)
    assert rag_index.renew(SessionLocal, [claimed]) == {claimed.token}
    for entry_id, _, _ in claimed.entries:
        with rs.session_for(SessionLocal, data.user_id) as db:
            workers.kill(db, data.user_id, "indexing", [entry_id])
    assert rag_index.renew(SessionLocal, [claimed]) == set()


def test_the_scheduler_job_indexes_every_account_side_by_side(data, monkeypatch):
    from app.services import scheduled_jobs

    other = rs.load(SessionLocal, make_user(SessionLocal))
    monkeypatch.setattr(get_settings(), "rag_embed_batch_size", 3)
    ctx = scheduled_jobs.JobContext(sessions=SessionLocal, trigger="manual", now=datetime.now(UTC).replace(tzinfo=None),
                                    worker_id="test", interval_minutes=10)
    summary = asyncio.run(scheduled_jobs.rag_index_job(ctx))
    assert summary["embedded"] >= len(data.facts) + len(other.facts) and not summary["errors"]
    assert summary["claimed"] >= (len(data.facts) + len(other.facts)) // 3
    for account in (data, other):
        assert {e.status for e in entries(account)} == {"ACTIVE"}


def test_the_indexer_settings_are_listed_for_the_account_and_the_server(data, client):
    out = client.get("/api/workers/settings").json()
    assert [f["name"] for f in out["account"]["indexing"]["fields"]] == ["auto", "max_attempts", "retry_seconds"]
    assert {n for n, x in out["server"].items() if x["worker"] == "indexing"} == {
        "rag_index_interval_minutes", "rag_embed_batch_size", "rag_index_max_entries_per_run", "rag_index_concurrency"}
    assert out["schedules"]["indexing"] is None or out["schedules"]["indexing"]["name"] == "rag_index"


def test_ai_orchestration_counts_the_queue_at_the_retrieval_index_node(data):
    from app.services import orchestration, orchestration_topology

    node = orchestration_topology.node("retrieval_index")
    assert node.workers_tab == "indexing" and not node.is_ai and orchestration_topology.validate() == []
    version_id = queued(data)
    claim(data, version_id, limit=2)
    with rs.session_for(SessionLocal, data.user_id) as db:
        counts = orchestration.live_counts(db, data.user_id)["retrieval_index"]
    assert counts["running"] == 2 and counts["queued"] == len(data.facts) - 2
