import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Job, SearchRun
from app.settings import get_settings
from app.workspace import extract_job_posting

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


def make_job(title="Lead AI Engineer", company=None):
    tag = uuid.uuid4().hex[:8]
    response = client.post("/api/jobs/from-url", json={
        "url": f"https://example.com/jobs/{tag}", "title": title, "company": company or f"Acme {tag}",
        "location": "Remote", "description": "Python FastAPI AWS and team leadership"})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def test_packet_flows_from_triage_to_tracker():
    assert client.put("/api/profile", json={
        "name": "Test Candidate", "headline": "Engineer", "email": "t@example.com", "phone": "123",
        "master_resume": "Built Python FastAPI services handling 10k requests per second\nLed 5 engineers on AWS",
        "target_roles": ["Lead AI Engineer"]}).status_code == 200
    client.post("/api/evidence", json={"claim": "Built Python FastAPI services at 10k rps", "company": "Acme",
                                       "skills": ["Python", "FastAPI"]})
    job = make_job()

    queue = client.get("/api/jobs/search", params={"queue": True, "q": job["company"]}).json()
    assert [item["id"] for item in queue["items"]] == [job["id"]]
    assert client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"}).json()["status"] == "shortlisted"
    assert client.get("/api/jobs/search", params={"queue": True, "q": job["company"]}).json()["total"] == 0
    assert any(item["id"] == job["id"] for item in client.get("/api/tailor/queue").json())
    assert client.get(f"/api/tailor/{job['id']}").json()["resume"] is None

    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    resume = client.get(f"/api/tailor/{job['id']}").json()["resume"]
    assert resume["reviewer_score"] == 86 and resume["edits"][0]["section"] == "SUMMARY"
    edited = client.patch(f"/api/resumes/{resume['id']}/edits/0",
                          json={"text": "Custom summary line", "status": "accepted"}).json()
    assert "Custom summary line" in edited["resume"]["markdown"]
    last = len(resume["edits"]) - 1
    bullet = resume["edits"][last]["after"]
    reverted = client.patch(f"/api/resumes/{resume['id']}/edits/{last}", json={"status": "reverted"}).json()
    assert f"- {bullet}" not in reverted["resume"]["markdown"]
    assert client.patch(f"/api/resumes/{resume['id']}/edits/99", json={"status": "accepted"}).status_code == 404

    waiting = client.get("/api/approvals").json()
    assert any(row["id"] == job["id"] and row["waiting"] for row in waiting)
    detail = client.get(f"/api/approvals/{job['id']}").json()
    assert len(detail["checklist"]) == 6
    assert client.get(f"/api/jobs/{job['id']}/apply-kit").status_code == 400
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200

    kit = client.get(f"/api/jobs/{job['id']}/apply-kit").json()
    assert [f["kind"] for f in kit["files"]] == ["PDF", "DOCX", "TXT"]
    for item in kit["files"]:
        assert client.get(item["href"]).status_code == 200

    assert client.post(f"/api/jobs/{job['id']}/applied", json={"recruiter_name": "Priya"}).status_code == 200
    tracker = client.get("/api/tracker").json()
    row = next(a for a in tracker["applications"] if a["job_id"] == job["id"])
    assert row["stage"] == "applied" and row["recruiter_name"] == "Priya"
    assert row["timeline"][0]["kind"] == "applied"

    moved = client.post(f"/api/applications/{row['id']}/stage", json={"stage": "screening"}).json()
    assert moved["stage"] == "screening" and moved["timeline"][-1]["title"] == "Moved to Screening"
    assert client.post(f"/api/applications/{row['id']}/stage", json={"stage": "nope"}).status_code == 422
    future = (datetime.now(UTC) + timedelta(days=3)).replace(tzinfo=None).isoformat()
    interview = client.post(f"/api/applications/{row['id']}/events",
                            json={"kind": "interview", "title": "Panel", "occurred_at": future}).json()
    assert interview["stage"] == "interviewing"
    assert client.post(f"/api/applications/{row['id']}/followup-draft").json()["draft"]
    assert client.put(f"/api/applications/{row['id']}/recruiter",
                      json={"recruiter_name": "Deepak", "recruiter_email": "d@example.com"}).json()["recruiter_name"] == "Deepak"

    overview = client.get("/api/workspace/overview").json()
    assert [f["label"] for f in overview["funnel"]][:2] == ["Discovered", "Shortlisted"]
    assert any(u["kind"] == "interview" for u in overview["upcoming"])
    today = client.get("/api/workspace/today").json()
    assert {"actions", "followups", "week", "funnel"} <= today.keys()
    summary = client.get("/api/workspace/summary").json()
    assert summary["counts"]["tracker"] >= 1


def test_skip_tailoring_uses_the_master_resume_unchanged():
    master = ("# Test Candidate\nEngineer\n## Experience\n### Lead | Acme | Remote | 2020 - 2024\n"
              "- Built Python FastAPI services handling 10k requests per second\n")
    assert client.put("/api/profile", json={"name": "Test Candidate", "headline": "Engineer", "email": "t@example.com",
                                            "phone": "123", "master_resume": master}).status_code == 200
    job = make_job()
    assert client.post(f"/api/jobs/{job['id']}/skip-tailoring").status_code == 409  # not shortlisted yet
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})

    out = client.post(f"/api/jobs/{job['id']}/skip-tailoring").json()
    assert out["untailored"] is True and out["reviewer_score"] == 86 and out["cover_letter"] == ""
    resume = client.get(f"/api/tailor/{job['id']}").json()["resume"]
    assert resume["untailored"] is True and resume["edits"] == []
    assert "### Lead | Acme | Remote | 2020 - 2024" in resume["markdown"]
    assert "- Built Python FastAPI services handling 10k requests per second" in resume["markdown"]
    assert client.patch(f"/api/resumes/{resume['id']}/edits/0", json={"status": "accepted"}).status_code == 404

    assert any(row["id"] == job["id"] and row["waiting"] for row in client.get("/api/approvals").json())
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/skip-tailoring").status_code == 409  # already approved
    assert client.post("/api/jobs/999999/skip-tailoring").status_code == 404


def test_bulk_tailor_processes_the_queue_in_batches(monkeypatch):
    import app.main as main

    assert client.put("/api/profile", json={"name": "Test Candidate", "headline": "Engineer",
                                            "master_resume": "Built Python FastAPI services at 10k rps"}).status_code == 200
    first, second, broken, has_packet, not_shortlisted = [make_job()["id"] for _ in range(5)]
    for job_id in (first, second, broken, has_packet):
        client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{has_packet}/tailor").status_code == 200

    real_tailor = main.tailor_job

    def flaky_tailor(db, job):
        if job.id == broken:
            raise RuntimeError("model timed out")
        return real_tailor(db, job)

    monkeypatch.setattr(main, "tailor_job", flaky_tailor)
    ids = [first, second, broken, has_packet, not_shortlisted, 999999]
    out = client.post("/api/jobs/bulk-tailor", json={"ids": ids}).json()
    assert [d["id"] for d in out["done"]] == [first] and out["remaining"] == [second, broken]
    assert out["skipped"] == 2 and out["missing"] == 1 and out["done"][0]["reviewer_score"] == 86

    out = client.post("/api/jobs/bulk-tailor", json={"ids": out["remaining"], "batch_size": 5}).json()
    assert [d["id"] for d in out["done"]] == [second] and out["remaining"] == []
    assert out["errors"] == [{"id": broken, "error": "model timed out"}]
    assert client.get(f"/api/tailor/{first}").json()["resume"]["untailored"] is False
    assert client.get(f"/api/tailor/{broken}").json()["resume"] is None

    skipped = client.post("/api/jobs/bulk-tailor", json={"ids": [broken, has_packet], "mode": "skip",
                                                         "regenerate": True, "batch_size": 5}).json()
    assert sorted(d["id"] for d in skipped["done"]) == sorted([broken, has_packet])
    assert all(d["untailored"] for d in skipped["done"])
    assert client.get(f"/api/tailor/{has_packet}").json()["resume"]["version"] == 2

    assert client.post("/api/jobs/bulk-tailor", json={"ids": []}).status_code == 422
    assert client.post("/api/jobs/bulk-tailor", json={"ids": [first], "mode": "nope"}).status_code == 422


def test_search_filters_unranked_jobs():
    tag = uuid.uuid4().hex[:8]
    ranked = make_job(company=f"Rankco {tag} A")
    unranked = make_job(company=f"Rankco {tag} B")
    assert client.post(f"/api/jobs/{ranked['id']}/rank").status_code == 200

    out = client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "unranked"}).json()
    assert [item["id"] for item in out["items"]] == [unranked["id"]] and out["total"] == 1
    selected = client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "unranked", "ids_only": True}).json()
    assert selected["ids"] == [unranked["id"]]
    assert [item["id"] for item in client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "all"}).json()["items"]] \
        == [ranked["id"], unranked["id"]]
    shortlisted = client.get("/api/jobs/search", params={"q": f"Rankco {tag}", "stage": "shortlisted"}).json()
    assert [item["id"] for item in shortlisted["items"]] == [ranked["id"]]


def test_triage_actions_and_filters():
    job = make_job(company=f"Talent Staffing {uuid.uuid4().hex[:6]}")
    assert client.post(f"/api/jobs/{job['id']}/triage", json={"action": "explode"}).status_code == 422
    hidden = client.get("/api/jobs/search", params={"q": job["company"], "hide_agencies": True}).json()
    assert hidden["total"] == 0
    assert client.post("/api/jobs/bulk-triage", json={"ids": [job["id"]], "action": "maybe"}).json()["changed"] == 1
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "maybe"
    assert client.get("/api/jobs/999999").status_code == 404


def test_search_profile_save_run_and_detail(monkeypatch):
    bad = client.put("/api/search/profile", json={"roles": ["AI"], "sites": ["myspace"]})
    assert bad.status_code == 422
    saved = client.put("/api/search/profile", json={"roles": ["AI Engineer"], "locations": ["Remote", "Pune"],
                                                    "sites": ["linkedin"], "schedule": "daily"})
    assert saved.status_code == 200
    started = []

    def fake_start(_db, payload):
        started.append(payload["location"])
        return SimpleNamespace(id=len(started), status="READY", input_json={"location": payload["location"]})

    monkeypatch.setattr("app.workspace.start_apify_search", fake_start)
    response = client.post("/api/search/profile/run")
    assert response.status_code == 200 and started == ["Remote", "Pune"]

    from tests.conftest import ensure_test_user

    db = SessionLocal()
    db.info["user_id"] = ensure_test_user().id  # rows created here belong to the account the API acts as
    run = SearchRun(external_run_id=uuid.uuid4().hex, actor_id="actor",
                    input_json={"searchTerms": ["AI Engineer"], "location": "Remote", "sites": ["linkedin"]},
                    status="SUCCEEDED", item_count=5, created_jobs=1)
    db.add(run)
    db.commit()
    db.add(Job(source="apify:linkedin", source_ref=uuid.uuid4().hex, title="AI Engineer", company="Pulled Co",
               search_run_id=run.id))
    db.commit()
    run_id = run.id
    db.close()
    detail = client.get(f"/api/search/apify/runs/{run_id}/detail").json()
    assert detail["counts"]["duplicates"] == 4 and detail["results"][0]["company"] == "Pulled Co"
    assert client.post(f"/api/search/apify/runs/{run_id}/rank").json()["ranked"] == 1


def test_tracker_stages_preferences_and_evidence_parsing():
    stages = client.put("/api/tracker/stages", json={"extra_stages": ["offer"]}).json()
    assert [s["id"] for s in stages["stages"]][-2:] == ["offer", "closed"]
    assert client.put("/api/tracker/stages", json={"extra_stages": ["party"]}).status_code == 422
    prefs = client.put("/api/preferences", json={"weekly_target": 12, "followup_days": 9}).json()
    assert prefs["weekly_target"] == 12 and prefs["followup_days"] == 9 and len(prefs["stage_models"]) == 4

    # Unique text: a persistent TEST_DATABASE_URL keeps claims from earlier runs.
    text = f"Cut cloud spend by 35% across 3 regions in 2024 ({uuid.uuid4().hex[:8]})"
    client.put("/api/profile", json={"master_resume": f"{text}\nshort"})
    parsed = client.post("/api/profile/resume/parse-evidence").json()
    assert parsed["added"] >= 1
    evidence = client.get("/api/evidence").json()
    claim = next(e for e in evidence if e["claim"] == text)
    assert claim["verified"] is False
    assert client.patch(f"/api/evidence/{claim['id']}", json={"verified": True}).json()["verified"] is True


def test_knowledge_edit_and_url_guards():
    created = client.post("/api/knowledge", json={"question": f"Notice period {uuid.uuid4().hex}", "answer": "60 days"}).json()
    updated = client.put(f"/api/knowledge/{created['id']}", json={"question": f"Notice period {uuid.uuid4().hex}?", "answer": "45 days",
                                                                   "verified": True})
    assert updated.status_code == 200 and updated.json()["verified"] is True
    blocked = client.post("/api/jobs/from-url", json={"url": "http://127.0.0.1:8000/admin"})
    assert blocked.status_code == 422


def test_board_import_errors_are_readable(monkeypatch):
    import httpx

    def missing_board(_db, token):
        request = httpx.Request("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr("app.main.import_greenhouse", missing_board)
    response = client.post("/api/import/greenhouse/nope")
    assert response.status_code == 404
    assert response.json()["detail"] == "No public Greenhouse board named 'nope'"


def test_rank_batch_ranks_unranked_jobs():
    job = make_job()
    ranked = client.post("/api/jobs/rank-batch").json()
    assert any(row["id"] == job["id"] and row["score"] == 80 for row in ranked)


def test_extract_job_posting_reads_json_ld():
    html = """<html><head><title>Careers</title><script type="application/ld+json">
    {"@type": "JobPosting", "title": "Staff ML Engineer", "hiringOrganization": {"name": "Quantiphi"},
     "jobLocation": {"address": {"addressLocality": "Bengaluru", "addressCountry": "IN"}},
     "description": "<p>Build <b>RAG</b> systems</p>"}</script></head><body></body></html>"""
    data = extract_job_posting(html)
    assert data["title"] == "Staff ML Engineer" and data["company"] == "Quantiphi"
    assert data["location"] == "Bengaluru, IN" and "RAG" in data["description"]


def test_tailor_and_approval_queues_filter_and_sort():
    assert client.put("/api/profile", json={"name": "Test Candidate", "headline": "Engineer",
                                            "master_resume": "Built Python FastAPI services at 10k rps"}).status_code == 200
    tag = uuid.uuid4().hex[:8]
    tailored, skipped, bare, approved = (make_job(title=f"Engineer {tag} {n}", company=f"Filter {tag} {n}")["id"]
                                         for n in range(4))
    for job_id in (tailored, skipped, bare, approved):
        client.post(f"/api/jobs/{job_id}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{tailored}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{skipped}/skip-tailoring").status_code == 200
    assert client.post(f"/api/jobs/{approved}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{approved}/approve").status_code == 200

    def tailor(**params):
        out = client.get("/api/tailor/queue", params={"q": tag, "with_facets": True, **params}).json()
        return {r["id"] for r in out["items"]}, out

    ids, out = tailor()
    assert ids == {tailored, skipped, bare} and out["total_all"] >= 3 and "Manual" in out["boards"]
    assert tailor(status="not_tailored")[0] == {bare}
    assert tailor(status="has_packet")[0] == {tailored, skipped}
    assert tailor(status="untailored")[0] == {skipped}
    assert tailor(min_review=1)[0] == {tailored, skipped}
    assert tailor(q=f"Filter {tag} 2")[0] == {bare}
    assert tailor(board="Greenhouse")[0] == set()
    # Without with_facets the endpoint still returns a plain list.
    assert isinstance(client.get("/api/tailor/queue", params={"q": tag}).json(), list)
    assert client.get("/api/tailor/queue", params={"status": "nope"}).status_code == 422
    assert client.get("/api/tailor/queue", params={"sort": "nope"}).status_code == 422

    def approvals(**params):
        return [r["id"] for r in client.get("/api/approvals", params={"q": tag, **params}).json()]

    assert set(approvals()) == {tailored, skipped, approved}
    assert approvals(status="approved") == [approved]
    assert set(approvals(status="waiting")) == {tailored, skipped}
    assert approvals(sort="company") == sorted([tailored, skipped, approved])
    assert client.get("/api/approvals", params={"status": "not_tailored"}).status_code == 422


def applied_application():
    assert client.put("/api/profile", json={"name": "Test Candidate", "headline": "Engineer",
                                            "master_resume": "Built Python FastAPI services at 10k rps"}).status_code == 200
    job = make_job()
    client.post(f"/api/jobs/{job['id']}/triage", json={"action": "shortlist"})
    assert client.post(f"/api/jobs/{job['id']}/tailor").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/applied", json={"recruiter_name": "Priya"}).status_code == 200
    return next(a for a in client.get("/api/tracker").json()["applications"] if a["job_id"] == job["id"])


def test_application_keeps_several_recruiters():
    row = applied_application()
    assert row["recruiters"] == [{"name": "Priya", "email": "", "phone": ""}]
    url = f"/api/applications/{row['id']}/recruiters"
    out = client.put(url, json={"recruiters": [
        {"name": " Karan Gupta ", "email": "Karan.Gupta@Example.com", "phone": "+91 98765-43210"},
        {"name": "Azhar Ahmed", "phone": "8904304811"},
        {"email": "talent@example.com"}]}).json()
    assert out["recruiters"] == [{"name": "Karan Gupta", "email": "Karan.Gupta@example.com", "phone": "+91 98765-43210"},
                                 {"name": "Azhar Ahmed", "email": "", "phone": "8904304811"},
                                 {"name": "", "email": "talent@example.com", "phone": ""}]
    assert out["recruiter_name"] == "Karan Gupta"  # the first recruiter is mirrored for older readers

    # The single-recruiter endpoint edits only the first one.
    out = client.put(f"/api/applications/{row['id']}/recruiter", json={"recruiter_name": "Deepak", "recruiter_email": ""}).json()
    assert [r["name"] for r in out["recruiters"]] == ["Deepak", "Azhar Ahmed", ""]
    assert out["recruiters"][0]["phone"] == "+91 98765-43210"

    assert client.put(url, json={"recruiters": [{"name": "X", "email": "not-an-email"}]}).status_code == 422
    assert client.put(url, json={"recruiters": [{"name": "X", "phone": "call me"}]}).status_code == 422
    assert client.put(url, json={"recruiters": [{}]}).status_code == 422
    assert client.put(url, json={"recruiters": [{"name": str(n)} for n in range(21)]}).status_code == 422
    assert client.put(url, json={"recruiters": []}).json()["recruiters"] == []


def test_interview_is_scheduled_in_its_time_zone_with_a_contact():
    row = applied_application()
    url = f"/api/applications/{row['id']}/events"
    local = (datetime.now(UTC) + timedelta(days=2)).replace(hour=16, minute=0, second=0, microsecond=0, tzinfo=None)
    out = client.post(url, json={"kind": "interview", "title": "Telephonic", "local_time": local.isoformat(),
                                 "timezone": "Asia/Kolkata", "contact_name": "Azhar Ahmed",
                                 "contact_email": "azhar@example.com", "contact_phone": "+91 8904304811"}).json()
    event = next(e for e in out["timeline"] if e["kind"] == "interview")
    assert event["at"] == (local - timedelta(hours=5, minutes=30)).isoformat()  # stored as UTC
    assert event["timezone"] == "Asia/Kolkata" and event["at_local"].endswith("16:00 IST")
    assert (event["contact_name"], event["contact_email"], event["contact_phone"]) == (
        "Azhar Ahmed", "azhar@example.com", "+91 8904304811")
    assert out["next_date"].endswith("16:00 IST")

    bad = {"kind": "interview", "title": "Panel", "local_time": local.isoformat()}
    assert client.post(url, json={**bad, "timezone": "Mars/Olympus"}).status_code == 422
    assert client.post(url, json=bad).status_code == 422  # a local time needs a zone
    assert client.post(url, json={**bad, "timezone": "UTC", "contact_email": "nope"}).status_code == 422
    # An offset-aware occurred_at is converted, not truncated.
    out = client.post(url, json={"kind": "note", "title": "Call", "occurred_at": "2026-09-16T16:00:00+05:30"}).json()
    assert any(e["at"] == "2026-09-16T10:30:00" for e in out["timeline"])
