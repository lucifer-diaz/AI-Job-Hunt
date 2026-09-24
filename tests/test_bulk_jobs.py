import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings

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


def test_bulk_rank_queues_tasks_and_never_ranks_in_the_request():
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


def test_bulk_delete_protects_submitted_jobs_and_cascades():
    ids = make_jobs(3, uuid.uuid4().hex[:8])
    tailored, applied, plain = ids
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
