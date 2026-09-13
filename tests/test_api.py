import os

os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"

from fastapi.testclient import TestClient
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import CandidateProfile, SearchRun


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_health_and_dashboard():
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    r = client.get("/")
    assert r.status_code == 200
    assert "Job Hunt OS" in r.text


def test_all_workspace_views_and_discovery_controls():
    client = TestClient(app)
    for path, heading in [
        ("/", "Dashboard"),
        ("/discovery", "Discovery"),
        ("/approvals", "Approvals"),
        ("/knowledge", "Knowledge Bank"),
        ("/settings", "Settings"),
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert f"<h1>{heading}</h1>" in response.text
    discovery = client.get("/discovery").text
    for control in [
        "apify-terms",
        "apify-location",
        "sitebox",
        "apify-max",
        "apify-hours",
        "apify-remote",
        "apify-easy",
        "apify-job-type",
        "apify-country",
        "apify-actor",
    ]:
        assert control in discovery
    assert "No discovery runs yet" in discovery
    assert "Retry status check" in discovery


def test_apify_filter_validation():
    client = TestClient(app)
    valid = {"search_terms": ["Engineer"], "sites": ["linkedin"]}
    for changes in [
        {"search_terms": []},
        {"sites": []},
        {"max_results": 0},
        {"max_results": 101},
        {"hours_old": 0},
        {"hours_old": 8761},
    ]:
        assert client.post("/api/search/apify", json={**valid, **changes}).status_code == 422


def test_settings_view_preserves_profile_and_application_controls():
    client = TestClient(app)
    response = client.get("/settings")
    assert response.status_code == 200
    for control in ["p-name", "p-resume", "providers", "approval_mode", "max_jobs_per_batch"]:
        assert f'id="{control}"' in response.text
    assert response.text.count("<!doctype html>") == 1
    assert 'aria-current="page">Settings</a>' in response.text
    assert 'id="providers"' not in client.get("/discovery").text


def test_views_and_ranking_use_persisted_application_settings(monkeypatch):
    from app.models import ApplicationSettings, Job

    with SessionLocal() as db:
        db.add(ApplicationSettings(id=1, active_provider="anthropic", max_jobs_per_batch=1))
        db.add_all([
            Job(title="Engineer", company="Example", source_ref=str(index))
            for index in range(2)
        ])
        db.commit()
    monkeypatch.setattr("app.main.rank_job", lambda db, job: None)
    client = TestClient(app)
    assert "LLM: anthropic" in client.get("/").text
    response = client.post("/api/jobs/rank-batch")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_failed_search_still_persists_filters(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.main.start_apify_search", fail)
    response = TestClient(app).post(
        "/api/search/apify",
        json={"search_terms": ["Platform Engineer"], "sites": ["indeed"], "max_results": 12},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "provider unavailable"
    with SessionLocal() as db:
        profile = db.get(CandidateProfile, 1)
        assert profile.search_preferences["search_terms"] == ["Platform Engineer"]
        assert profile.search_preferences["max_results"] == 12


def test_successful_search_and_inspectable_run(monkeypatch):
    def succeed(db, payload):
        run = SearchRun(
            external_run_id="external-1",
            actor_id=payload["actor_id"] or "default/actor",
            input_json={"searchTerms": payload["search_terms"], "sites": payload["sites"]},
            status="RUNNING",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr("app.main.start_apify_search", succeed)
    client = TestClient(app)
    response = client.post(
        "/api/search/apify",
        json={"search_terms": ["AI Engineer"], "sites": ["linkedin"], "actor_id": "me/jobs"},
    )
    assert response.status_code == 200
    run_id = response.json()["id"]
    runs = client.get("/api/search/apify/runs").json()
    assert runs[0]["id"] == run_id
    assert runs[0]["input"] == {"searchTerms": ["AI Engineer"], "sites": ["linkedin"]}


def test_saving_profile_does_not_reset_search_preferences():
    client = TestClient(app)
    with SessionLocal() as db:
        db.add(CandidateProfile(id=1, search_preferences={"search_terms": ["SRE"]}))
        db.commit()
    assert client.put("/api/profile", json={"name": "Ada"}).status_code == 200
    profile = client.get("/api/profile").json()
    assert profile["name"] == "Ada"
    assert profile["search_preferences"] == {"search_terms": ["SRE"]}
