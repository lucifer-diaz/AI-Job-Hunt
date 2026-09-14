import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def make_jobs(count, tag):
    ids = []
    for i in range(count):
        job = client.post("/api/jobs/from-url", json={"url": f"https://example.com/bulk/{tag}/{i}", "title": f"Engineer {i}",
                                                      "company": f"Bulk Co {tag}", "description": "Python"}).json()["job"]
        ids.append(job["id"])
    return ids


def test_select_all_matching_returns_every_id():
    tag = uuid.uuid4().hex[:8]
    ids = make_jobs(3, tag)
    out = client.get("/api/jobs/search", params={"q": f"Bulk Co {tag}", "per_page": 1, "ids_only": True}).json()
    assert sorted(out["ids"]) == sorted(ids) and out["total"] == 3


def test_bulk_rank_batches_and_reports_remaining(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_jobs_per_batch", 2)
    ids = make_jobs(3, uuid.uuid4().hex[:8])
    first = client.post("/api/jobs/bulk-rank", json={"ids": ids + [999999]}).json()
    assert len(first["ranked"]) == 2 and len(first["remaining"]) == 1 and first["errors"] == []
    second = client.post("/api/jobs/bulk-rank", json={"ids": first["remaining"]}).json()
    assert [r["score"] for r in second["ranked"]] == [80] and second["remaining"] == []
    again = client.post("/api/jobs/bulk-rank", json={"ids": ids}).json()
    assert again["ranked"] == [] and again["skipped"] == 3  # already ranked
    assert len(client.post("/api/jobs/bulk-rank?rerank=true", json={"ids": ids}).json()["ranked"]) == 2


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
