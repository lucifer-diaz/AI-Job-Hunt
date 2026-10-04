import uuid
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Job, SearchRun
from app.services.sources import dedupe_key, import_apify_items
from app.services.user_settings import set_user_keys
from tests.conftest import ensure_test_user

client = TestClient(app)


class FakeApify:
    """Stands in for the Apify REST API; runs can change status between calls."""

    def __init__(self, tag: str):
        self.tag = tag
        self.company = f"Sync Co {tag}"
        self.runs = {}
        self.items = {}
        self.lookups_fail = False

    ACTORS: ClassVar[dict[str, tuple[str, str, str]]] = {"ACT1": ("openclawai", "job-board-scraper", "Multi Job Board"),
              "ACT2": ("blackfalcondata", "wellfound-scraper", "Wellfound")}

    def add_run(self, status="SUCCEEDED", items=None, input_=None, act="ACT1"):
        run_id = uuid.uuid4().hex[:17]
        self.runs[run_id] = {"id": run_id, "actId": act, "status": status, "startedAt": "2026-09-14T10:00:00.000Z",
                             "finishedAt": "2026-09-14T10:02:00.000Z", "defaultDatasetId": f"DS{run_id}",
                             "defaultKeyValueStoreId": f"KV{run_id}", "meta": {"origin": "WEB"}}
        self.items[f"DS{run_id}"] = items or []
        self.runs[run_id]["_input"] = input_ or {"searchTerm": "Staff AI Engineer", "location": "Pune"}
        return run_id

    def __call__(self, path, params=None, token=None, **telemetry):  # telemetry kwargs: operation, metadata
        assert token == "test-token", "Apify calls must use the account's own token"
        if path == "/actor-runs":
            return {"data": {"items": [dict(r) for r in reversed(list(self.runs.values()))]}}
        if path.startswith("/actor-runs/"):
            return {"data": dict(self.runs[path.rsplit("/", 1)[1]])}
        if path.startswith("/acts/") and self.lookups_fail:
            raise httpx.ConnectError("Apify unreachable")
        if path.startswith("/acts/") and path.split("/")[2] in self.ACTORS:
            username, name, title = self.ACTORS[path.split("/")[2]]
            return {"data": {"username": username, "name": name, "title": title}}
        if path.startswith("/datasets/") and path.endswith("/items"):
            return self.items[path.split("/")[2]]
        if path.startswith("/datasets/"):
            return {"data": {"itemCount": len(self.items[path.split("/")[2]])}}
        if path.startswith("/key-value-stores/"):
            return next(r["_input"] for r in self.runs.values() if path.split("/")[2] == r["defaultKeyValueStoreId"])
        raise AssertionError(f"unexpected Apify call {path}")


def set_apify_key(value):
    with SessionLocal() as db:
        set_user_keys(db, {"apify": value}, user_id=ensure_test_user().id)


@pytest.fixture
def apify(monkeypatch):
    fake = FakeApify(uuid.uuid4().hex[:8])
    monkeypatch.setattr("app.services.sources.apify_get", fake)
    set_apify_key("test-token")
    yield fake
    set_apify_key("")


def listing(fake, site, ref, title="Staff AI Engineer", location="Pune, Maharashtra, India"):
    return {"id": f"{ref}-{fake.tag}", "site": site, "title": title, "company": fake.company, "location": location,
            "job_url": f"https://example.test/{site}/{ref}-{fake.tag}", "description": "Build agents."}


def test_cross_actor_duplicates_are_skipped(apify):
    db = SessionLocal()
    db.info["user_id"] = ensure_test_user().id
    first = import_apify_items(db, [listing(apify, "linkedin", "li1")])
    same_job_other_board = listing(apify, "indeed", "in9", location="Pune, MH")
    second = import_apify_items(db, [same_job_other_board, listing(apify, "indeed", "in9"),
                                     listing(apify, "indeed", "in2", title="Principal ML Engineer")])
    db.close()
    assert first["created"] == 1
    assert second["created"] == 1 and second["duplicates"] == 2
    assert dedupe_key("AI Lead", "Acme, Inc.", "Pune, India") == dedupe_key("ai lead", "Acme Inc", "Pune")


def test_import_remote_runs_with_input_dedupe_and_pull_again(apify):
    run_id = apify.add_run(items=[listing(apify, "linkedin", "a1"), listing(apify, "indeed", "b1", location="Pune"),
                                  listing(apify, "linkedin", "c1", title="Head of AI"), {"title": "no company"}])
    listed = client.get("/api/search/apify/remote-runs").json()["runs"]
    row = next(r for r in listed if r["external_run_id"] == run_id)
    assert row["actor_id"] == "openclawai/job-board-scraper" and row["item_count"] == 4 and row["importable"]

    out = client.post("/api/search/apify/import", json={"external_run_ids": [run_id]}).json()
    result = out["results"][0]
    assert result["ok"] and result["completed"]
    assert result["import"] == {"seen": 4, "normalized": 3, "created": 2, "skipped": 1, "duplicates": 1,
                                "not_matching": 0, "previously_removed": 0, "over_limit": 0}

    detail = client.get(f"/api/search/apify/runs/{result['id']}/detail").json()
    assert ["Role", "Staff AI Engineer"] in detail["context"] and ["Location", "Pune"] in detail["context"]
    assert detail["counts"]["new"] == 2

    row = next(r for r in client.get("/api/search/apify/remote-runs").json()["runs"] if r["external_run_id"] == run_id)
    assert row["imported"] and not row["importable"] and row["local_run_id"] == result["id"]

    again = client.get(f"/api/search/apify/runs/{result['id']}?reimport=true").json()
    assert again["import"]["created"] == 0 and again["import"]["duplicates"] == 3 and again["created_jobs"] == 2

    assert client.post("/api/search/apify/import", json={"external_run_ids": ["not a run!"]}).status_code == 422


def test_refresh_pending_imports_finished_runs_and_keeps_partial_results(apify):
    running = apify.add_run(status="RUNNING", items=[listing(apify, "linkedin", "p1")])
    timed_out = apify.add_run(status="TIMED-OUT", items=[listing(apify, "naukri", "t1", title="AI Architect")])
    client.post("/api/search/apify/import", json={"external_run_ids": [running, timed_out]})
    db = SessionLocal()
    tracked = db.scalar(select(SearchRun).where(SearchRun.external_run_id == running))
    db.close()
    assert tracked.completed_at is None

    apify.runs[running]["status"] = "SUCCEEDED"
    out = client.post("/api/search/apify/runs/refresh-pending").json()
    mine = [r for r in out["results"] if r.get("external_run_id") == running]
    assert mine and mine[0]["completed"] and mine[0]["import"]["created"] == 1

    timed = next(r for r in client.get("/api/search/apify/runs").json() if r["external_run_id"] == timed_out)
    assert timed["created_jobs"] == 1 and timed["error"] == "TIMED-OUT"


def test_sync_requires_the_accounts_own_token():
    set_apify_key("")
    assert client.get("/api/search/apify/remote-runs").status_code == 400
    assert client.post("/api/search/apify/runs/refresh-pending").status_code == 400


def test_a_startup_actor_run_is_imported_with_its_own_field_names(apify):
    wellfound = {"id": f"wf-{apify.tag}", "title": "Founding AI Engineer", "companyName": apify.company,
                 "locationNames": ["San Francisco"], "remote": True, "portalUrl": f"https://wellfound.com/jobs/{apify.tag}",
                 "description": "Ship the agent platform.", "compensation": "$180k – $220k • 0.5%"}
    run_id = apify.add_run(items=[wellfound, {"title": "no company"}], act="ACT2",
                           input_={"query": "AI Engineer", "maxResults": 20})
    result = client.post("/api/search/apify/import", json={"external_run_ids": [run_id]}).json()["results"][0]
    assert result["ok"] and result["import"]["created"] == 1 and result["import"]["skipped"] == 1
    detail = client.get(f"/api/search/apify/runs/{result['id']}/detail").json()
    job = detail["results"][0]
    assert (job["title"], job["company"]) == ("Founding AI Engineer", apify.company)
    assert ["Keywords", "AI Engineer"] in detail["context"]


def test_posting_dates_are_kept_and_an_unresolved_actor_is_looked_up_again(apify):
    wellfound = {"id": f"wf2-{apify.tag}", "title": "Staff Data Engineer", "companyName": apify.company,
                 "locationNames": ["Berlin"], "portalUrl": f"https://wellfound.com/jobs/2{apify.tag}",
                 "description": "Pipelines.", "postedAt": "2026-09-18"}
    run_id = apify.add_run(items=[wellfound], act="ACT2")
    apify.lookups_fail = True  # the actor's name cannot be resolved when the run is first imported
    first = client.post("/api/search/apify/import", json={"external_run_ids": [run_id]}).json()["results"][0]
    assert not first["ok"] and "look up" in first["error"]
    with SessionLocal() as db:
        run = db.scalar(select(SearchRun).where(SearchRun.external_run_id == run_id))
        # Nothing is imported under a guessed mapping; the run waits for the next try.
        assert run.actor_id == "ACT2" and run.completed_at is None
        assert db.scalar(select(Job).where(Job.company == apify.company)) is None

    apify.lookups_fail = False
    second = client.post("/api/search/apify/import", json={"external_run_ids": [run_id]}).json()["results"][0]
    assert second["ok"] and second["import"]["created"] == 1
    with SessionLocal() as db:
        assert db.get(SearchRun, second["id"]).actor_id == "blackfalcondata/wellfound-scraper"
        job = db.scalar(select(Job).where(Job.company == apify.company, Job.title == "Staff Data Engineer"))
        assert job.posted_at is not None and job.posted_at.date().isoformat() == "2026-09-18"
        assert job.location == "Berlin"
