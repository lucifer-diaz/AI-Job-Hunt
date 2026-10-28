import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from tests.resume_support import use_resume_text

client = TestClient(app)


# A posting long enough to rank: app.services.ranker holds back listings with nothing to read.
POSTING = ("Lead Python platform teams building services on Kubernetes and AWS. You will own the roadmap for "
           "our data platform, mentor five engineers, and work with product to ship reliably. We look for expe"
           "rience with FastAPI, distributed systems and a track record of leading delivery.")

@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def make_jobs(count, tag):
    ids = []
    for i in range(count):
        job = client.post("/api/jobs/from-url", json={"url": f"https://example.com/bulk/{tag}/{i}", "title": f"Engineer {i}",
                                                      "company": f"Bulk Co {tag}", "description": POSTING}).json()["job"]
        ids.append(job["id"])
    return ids


def test_select_all_matching_returns_every_id():
    tag = uuid.uuid4().hex[:8]
    ids = make_jobs(3, tag)
    out = client.get("/api/jobs/search", params={"q": f"Bulk Co {tag}", "per_page": 1, "ids_only": True}).json()
    assert sorted(out["ids"]) == sorted(ids) and out["total"] == 3


def test_bulk_rank_queues_tasks_and_never_ranks_in_the_request(monkeypatch):
    monkeypatch.setattr(get_settings(), "jd_parse_enabled", False)  # without parsing there is nothing to wait for
    ids = make_jobs(3, uuid.uuid4().hex[:8])
    response = client.post("/api/jobs/bulk-rank", json={"ids": ids + [999999]})
    assert response.status_code == 202
    first = response.json()
    assert first["queued"] == ids and first["skipped"] == [{"id": 999999, "reason": "not found"}]
    assert [t["status"] for t in first["tasks"]] == ["PENDING"] * 3 and all(t["force"] is False for t in first["tasks"])
    assert all(client.get(f"/api/jobs/{i}").json()["fit"] is None for i in ids)

    again = client.post("/api/jobs/bulk-rank?rerank=true", json={"ids": ids}).json()
    assert again["queued"] == ids and [t["id"] for t in again["tasks"]] == [t["id"] for t in first["tasks"]]
    assert all(t["force"] for t in again["tasks"])  # a user request upgrades the waiting task instead of duplicating it

    from app.db import SessionLocal
    from app.models import Job
    with SessionLocal() as db:
        db.get(Job, ids[0]).fit_score = 80.0
        db.commit()
    unranked_only = client.post("/api/jobs/bulk-rank", json={"ids": ids}).json()
    assert unranked_only["queued"] == ids  # jobs still waiting for the ranker stay queued


def _set_parse(job_id, status, *, current=True):
    from app.db import SessionLocal
    from app.models import Job
    from app.services import job_description
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.structured_description_status = status
        job.structured_description = {"title": job.title} if status == "PARSED" else None
        job.structured_description_hash = job_description.description_hash(job.description) if current else "old"
        db.commit()


def _finish_parse_tasks(job_ids):
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models import JobDescriptionParseTask
    with SessionLocal() as db:
        db.execute(update(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id.in_(job_ids))
                   .values(status="FAILED"))
        db.commit()


def test_bulk_rank_leaves_out_jobs_that_are_not_parsed():
    parsed, failed, fresh = make_jobs(3, uuid.uuid4().hex[:8])
    _finish_parse_tasks([parsed, failed, fresh])
    _set_parse(parsed, "PARSED")
    _set_parse(failed, "NEEDS_REVIEW")
    out = client.post("/api/jobs/bulk-rank?rerank=true", json={"ids": [parsed, failed, fresh]}).json()
    assert out["queued"] == [parsed]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {failed: "not parsed yet", fresh: "not parsed yet"}


def test_bulk_parse_queues_unparsed_and_failed_jobs_only():
    tag = uuid.uuid4().hex[:8]
    parsed, failed, stale, busy = make_jobs(4, tag)
    _finish_parse_tasks([parsed, failed, stale])  # `busy` keeps the parse task it was created with
    _set_parse(parsed, "PARSED")
    _set_parse(failed, "NEEDS_REVIEW")
    _set_parse(stale, "PARSED", current=False)  # parsed, but the description changed since
    response = client.post("/api/jobs/bulk-parse", json={"ids": [parsed, failed, stale, busy, 999999]})
    assert response.status_code == 202
    out = response.json()
    assert out["queued"] == [failed, stale]
    assert {s["id"]: s["reason"] for s in out["skipped"]} == {
        parsed: "already parsed", busy: "already parsing", 999999: "not found"}
    states = {r["id"]: r["description_state"] for r in client.get("/api/jobs/search", params={"q": f"Bulk Co {tag}"}).json()["items"]}
    assert states[failed] == states[stale] == "PARSING"
    again = client.post("/api/jobs/bulk-parse", json={"ids": [failed]}).json()
    assert again["queued"] == [] and again["skipped"] == [{"id": failed, "reason": "already parsing"}]


def test_select_all_matching_reports_parsed_ids():
    tag = uuid.uuid4().hex[:8]
    parsed, fresh = make_jobs(2, tag)
    _finish_parse_tasks([parsed])
    _set_parse(parsed, "PARSED")
    out = client.get("/api/jobs/search", params={"q": f"Bulk Co {tag}", "ids_only": True}).json()
    assert out["parsed_ids"] == [parsed]
    assert client.get("/api/jobs/search", params={"q": f"Bulk Co {tag}"}).json()["parse_enabled"] is True


def test_bulk_delete_protects_submitted_jobs_and_cascades():
    ids = make_jobs(3, uuid.uuid4().hex[:8])
    tailored, applied, plain = ids
    # Tailoring needs resume content to present: an empty resume is refused before any model call.
    use_resume_text(client, "Built Python FastAPI services at 10k rps")
    for job_id in (tailored, applied):
        client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
        assert client.post(f"/api/jobs/{job_id}/tailor").status_code == 200
    client.post(f"/api/jobs/{applied}/approve")
    assert client.post(f"/api/jobs/{applied}/applied", json={}).status_code == 200

    out = client.post("/api/jobs/bulk-delete", json={"ids": ids}).json()
    assert out == {"deleted": 2, "protected": [applied], "missing": 0}
    assert client.get(f"/api/jobs/{plain}").status_code == 404
    assert client.get(f"/api/tailor/{tailored}").status_code == 404
    assert client.get(f"/api/jobs/{applied}").status_code == 200

    forced = client.post("/api/jobs/bulk-delete", json={"ids": [applied], "include_submitted": True}).json()
    assert forced["deleted"] == 1
    assert all(a["job_id"] != applied for a in client.get("/api/tracker").json()["applications"])
    assert client.post("/api/jobs/bulk-delete", json={"ids": []}).status_code == 422
