"""Saved search setups: many named searches per account, run on demand or on their own schedule."""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import CandidateProfile, SavedSearch, SearchRun
from app.settings import get_settings
from tests.conftest import ensure_test_user, make_user

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")


@pytest.fixture(autouse=True)
def no_setups():
    """Start from an account with no setups, so the first read migrates the older single profile."""
    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        for setup in db.scalars(select(SavedSearch)).all():
            db.delete(setup)
        profile = db.scalar(select(CandidateProfile))
        if profile:
            profile.search_preferences = {k: v for k, v in (profile.search_preferences or {}).items()
                                          if k != "search_profile_migrated"}
        db.commit()


def setup_body(name, **overrides):
    settings = {"roles": ["AI Engineer"], "locations": ["Remote"], "sites": ["linkedin"],
                **overrides.pop("settings", {})}
    return {"name": name, "settings": settings, "schedule_kind": "manual", **overrides}


def listed():
    return client.get("/api/search/setups").json()["setups"]


def test_the_older_single_search_profile_becomes_the_first_setup():
    client.put("/api/search/profile", json={"roles": ["Staff Engineer"], "locations": ["Pune"],
                                            "sites": ["linkedin"], "schedule": "daily"})
    setups = listed()
    assert [s["name"] for s in setups] == ["My search"]
    assert setups[0]["settings"]["roles"] == ["Staff Engineer"]
    assert setups[0]["schedule_kind"] == "daily" and setups[0]["next_run_at"], "a daily profile stays daily"

    # The move happens once: a deleted setup does not come back.
    assert client.delete(f"/api/search/setups/{setups[0]['id']}").status_code == 200
    assert listed() == []


def test_setups_are_created_renamed_duplicated_and_deleted():
    first = client.post("/api/search/setups", json=setup_body("Remote AI")).json()["setup"]
    second = client.post("/api/search/setups", json=setup_body("Mumbai leadership", settings={"locations": ["Mumbai, Maharashtra, India"]}))
    assert second.status_code == 200
    assert {s["name"] for s in listed()} == {"Remote AI", "Mumbai leadership"}

    assert client.post("/api/search/setups", json=setup_body("remote ai")).status_code == 409, "names are unique per account"

    renamed = client.put(f"/api/search/setups/{first['id']}", json=setup_body("Remote AI · EU", settings={"roles": ["ML Engineer"]}))
    assert renamed.json()["setup"]["name"] == "Remote AI · EU"
    assert renamed.json()["setup"]["settings"]["roles"] == ["ML Engineer"]

    copy = client.post(f"/api/search/setups/{first['id']}/duplicate").json()["setup"]
    assert copy["name"] == "Remote AI · EU copy" and copy["settings"]["roles"] == ["ML Engineer"]
    assert client.post(f"/api/search/setups/{first['id']}/duplicate").json()["setup"]["name"] == "Remote AI · EU copy 2"

    assert client.delete(f"/api/search/setups/{copy['id']}").status_code == 200
    assert client.get("/api/search/setups").json()["setups"] and copy["id"] not in [s["id"] for s in listed()]
    assert client.delete(f"/api/search/setups/{copy['id']}").status_code == 404


def test_a_setup_is_saved_with_its_own_schedule():
    weekly = client.post("/api/search/setups", json=setup_body(
        "Weekday mornings", schedule_kind="weekly", weekdays=[4, 0, 0], time_of_day="07:30",
        timezone="Asia/Kolkata")).json()["setup"]
    assert weekly["weekdays"] == [0, 4] and weekly["schedule_label"] == "Mon, Fri at 07:30 Asia/Kolkata"
    assert datetime.fromisoformat(weekly["next_run_at"]) > datetime.utcnow()

    paused = client.put(f"/api/search/setups/{weekly['id']}", json=setup_body(
        "Weekday mornings", schedule_kind="weekly", weekdays=[0], time_of_day="07:30", enabled=False)).json()["setup"]
    assert paused["next_run_at"] is None and paused["schedule_label"] == "Paused"

    for bad in ({"schedule_kind": "hourly"}, {"schedule_kind": "weekly", "weekdays": []},
                {"schedule_kind": "daily", "time_of_day": "7am"},
                {"schedule_kind": "daily", "timezone": "Mars/Olympus"},
                {"schedule_kind": "interval", "every_hours": 500}):
        assert client.post("/api/search/setups", json=setup_body(f"Bad {uuid.uuid4().hex[:6]}", **bad)).status_code == 422
    assert client.post("/api/search/setups", json=setup_body("  ")).status_code == 422
    assert client.post("/api/search/setups", json=setup_body("Unknown board", settings={"sites": ["myspace"]})).status_code == 422


def test_running_a_setup_ties_its_runs_back_to_it(monkeypatch):
    setup = client.post("/api/search/setups", json=setup_body(
        "Two cities", settings={"locations": ["Remote", "Pune"]})).json()["setup"]
    started = []

    def fake_start(_db, payload):
        started.append(payload["location"])
        run = SearchRun(external_run_id=uuid.uuid4().hex, actor_id="openclawai/job-board-scraper",
                        input_json={"location": payload["location"]}, status="READY")
        _db.add(run)
        _db.commit()
        return run

    monkeypatch.setattr("app.workspace.start_apify_search", fake_start)
    out = client.post(f"/api/search/setups/{setup['id']}/run").json()
    assert started == ["Remote", "Pune"] and len(out["runs"]) == 2
    assert out["setup"]["last_run_at"], "running a setup records when it last ran"

    runs = client.get("/api/search/apify/runs").json()
    started_here = [r for r in runs if r["id"] in [x["id"] for x in out["runs"]]]
    assert {r["saved_search_id"] for r in started_here} == {setup["id"]}
    assert {r["saved_search_name"] for r in started_here} == {"Two cities"}

    assert client.post("/api/search/setups/999999/run").status_code == 404


def test_one_account_cannot_see_or_run_anothers_setup():
    theirs = make_user(SessionLocal, email=f"other-{uuid.uuid4().hex[:8]}@example.com")
    with SessionLocal() as db:
        db.add(SavedSearch(user_id=theirs, name="Their search", settings={"roles": ["Engineer"]}, weekdays=[]))
        db.commit()
        other_id = db.scalar(select(SavedSearch.id).where(SavedSearch.user_id == theirs))

    assert other_id not in [s["id"] for s in listed()]
    assert client.get("/api/search/setups").status_code == 200
    assert client.put(f"/api/search/setups/{other_id}", json=setup_body("Mine now")).status_code == 404
    assert client.delete(f"/api/search/setups/{other_id}").status_code == 404
    assert client.post(f"/api/search/setups/{other_id}/run").status_code == 404
    assert client.post(f"/api/search/setups/{other_id}/duplicate").status_code == 404
