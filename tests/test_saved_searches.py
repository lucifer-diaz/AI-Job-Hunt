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
        assert payload["roles"] == ["AI Engineer"] and payload["search_params"]["max_jobs"] == 20
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


def actor(actor_id, **extra):
    return {"id": actor_id, "name": actor_id, "fan_out": "role", "input_template": {}, **extra}


def by_name():
    return {s["name"]: s for s in listed()}


def test_actors_a_person_adds_are_listed_in_every_setup():
    mine = actor("me/jobs", input_template={"q": "{{role}} {{skill}}"})
    first = client.post("/api/search/setups", json=setup_body("Remote AI", changed_actors=["me/jobs"], settings={
        "custom_actors": [mine], "actors": ["me/jobs"]})).json()["setup"]
    second = client.post("/api/search/setups", json=setup_body("Mumbai")).json()["setup"]
    assert second["settings"]["custom_actors"] == [mine], "a new setup lists the actors already added"
    assert "me/jobs" not in second["settings"]["actors"], "a shared actor arrives unticked"

    edited, other = {**mine, "name": "Renamed"}, actor("me/other")
    client.put(f"/api/search/setups/{second['id']}", json=setup_body(
        "Mumbai", changed_actors=["me/jobs", "me/other"], settings={"custom_actors": [edited, other]}))
    assert by_name()["Remote AI"]["settings"]["custom_actors"] == [edited, other], "an edit and an addition reach every setup"
    assert by_name()["Remote AI"]["settings"]["actors"] == ["me/jobs"], "ticking stays per setup"

    client.put(f"/api/search/setups/{second['id']}", json=setup_body(
        "Mumbai", removed_actors=["me/jobs"], settings={"custom_actors": [other]}))
    assert by_name()["Remote AI"]["settings"]["custom_actors"] == [other], "a removal reaches every setup"
    assert by_name()["Remote AI"]["settings"]["actors"] == []
    assert first["id"]


def test_a_stale_form_neither_deletes_brings_back_nor_reverts_an_actor():
    a, b = actor("me/a"), actor("me/b")
    one = client.post("/api/search/setups", json=setup_body("One", changed_actors=["me/a"], settings={"custom_actors": [a]})).json()["setup"]
    two = client.post("/api/search/setups", json=setup_body("Two")).json()["setup"]
    stale_two = two["settings"]  # loaded now: lists a, not b
    client.put(f"/api/search/setups/{one['id']}", json=setup_body("One", changed_actors=["me/b", "me/a"], settings={
        "custom_actors": [{**a, "name": "A2"}, b]}))
    client.put(f"/api/search/setups/{two['id']}", json=setup_body("Two", settings={**stale_two, "roles": ["ML Engineer"]}))
    assert by_name()["One"]["settings"]["custom_actors"] == [{**a, "name": "A2"}, b], "the stale save changed nothing"

    client.put(f"/api/search/setups/{one['id']}", json=setup_body("One", removed_actors=["me/a"], settings={"custom_actors": [b]}))
    client.put(f"/api/search/setups/{two['id']}", json=setup_body("Two", settings={**stale_two, "actors": ["openclawai/job-board-scraper", "me/a"]}))
    setups = by_name()
    assert setups["Two"]["settings"]["custom_actors"] == [b], "a stale form does not bring a removed actor back"
    assert setups["Two"]["settings"]["actors"] == ["openclawai/job-board-scraper"], "nor keeps it ticked"


def test_removing_the_last_ticked_actor_pauses_that_setups_schedule_and_never_runs_the_defaults():
    from app.services.actors import plan_runs

    a = actor("me/a")
    one = client.post("/api/search/setups", json=setup_body("One", changed_actors=["me/a"], settings={"custom_actors": [a]})).json()["setup"]
    two = client.post("/api/search/setups", json=setup_body("Two", schedule_kind="daily", settings={"custom_actors": [a], "actors": ["me/a"]})).json()["setup"]
    assert two["enabled"] and two["settings"]["actors"] == ["me/a"]
    out = client.put(f"/api/search/setups/{one['id']}", json=setup_body("One", removed_actors=["me/a"])).json()
    assert out["paused_setups"] == ["Two"]
    paused = by_name()["Two"]
    assert paused["enabled"] is False and paused["settings"]["actors"] == []
    with pytest.raises(ValueError, match="at least one Apify actor"):
        plan_runs(paused["settings"])


def test_the_account_holds_at_most_25_of_its_own_actors():
    many = [actor(f"me/a{n}") for n in range(25)]
    one = client.post("/api/search/setups", json=setup_body("One", changed_actors=[a["id"] for a in many],
                                                            settings={"custom_actors": many})).json()["setup"]
    refused = client.post("/api/search/setups", json=setup_body("Two", changed_actors=["me/extra"],
                                                                 settings={"custom_actors": [actor("me/extra")]}))
    assert refused.status_code == 422 and "up to 25" in refused.json()["detail"]
    assert {s["name"] for s in listed()} == {"One"}, "nothing was saved"
    assert client.put(f"/api/search/setups/{one['id']}", json=setup_body("One · renamed", settings={"custom_actors": many})).status_code == 200


def test_setups_saved_before_sharing_get_each_others_actors():
    a = {"id": "me/a", "name": "A", "fan_out": "role", "input_template": {}}
    b = {"id": "me/b", "name": "B", "fan_out": "role", "input_template": {}}
    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        base = {"roles": ["AI Engineer"], "locations": ["Remote"], "sites": ["linkedin"]}
        db.add_all([SavedSearch(name="One", settings={**base, "custom_actors": [a], "actors": ["me/a"]}, schedule_kind="manual",
                                every_hours=24, time_of_day="07:00", weekdays=[], timezone="UTC", enabled=True),
                    SavedSearch(name="Two", settings={**base, "custom_actors": [b], "actors": ["me/b"]}, schedule_kind="manual",
                                every_hours=24, time_of_day="07:00", weekdays=[], timezone="UTC", enabled=True)])
        db.commit()
    setups = {s["name"]: s["settings"] for s in listed()}
    assert setups["One"]["custom_actors"] == setups["Two"]["custom_actors"] == [a, b]
    assert setups["One"]["actors"] == ["me/a"] and setups["Two"]["actors"] == ["me/b"]


def test_more_than_25_actors_from_before_sharing_are_kept_and_still_save():
    base = {"roles": ["AI Engineer"], "locations": ["Remote"], "sites": ["linkedin"]}
    with SessionLocal() as db:
        db.info["user_id"] = ensure_test_user().id
        for n in range(3):
            db.add(SavedSearch(name=f"Old {n}", settings={**base, "custom_actors": [actor(f"me/{n}-{i}") for i in range(10)]},
                               schedule_kind="manual", every_hours=24, time_of_day="07:00", weekdays=[], timezone="UTC", enabled=True))
        db.commit()
    setups = listed()
    assert all(len(s["settings"]["custom_actors"]) == 30 for s in setups), "none is dropped"
    first = setups[0]
    body = setup_body(first["name"], settings={**first["settings"], "roles": ["ML Engineer"]})
    assert client.put(f"/api/search/setups/{first['id']}", json=body).status_code == 200, "an unrelated change still saves"


def test_an_actors_skill_fields_are_suggested_the_primary_skill_placeholders():
    from app.workspace import GUESSES

    guess = lambda name: next((value for pattern, value in GUESSES if pattern.match(name)), "")  # noqa: E731
    assert [guess(n) for n in ("skill", "primarySkill", "skills", "techStack", "skillsTerms")] == \
        ["{{skill}}", "{{skill}}", "{{skills}}", "{{skills}}", "{{skills}}"]
