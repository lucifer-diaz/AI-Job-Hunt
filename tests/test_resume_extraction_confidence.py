"""Resume extraction V2, Phase 7: each fact's confidence, from checkable signals (confidence.assess), and "accept all
high-confidence", which verifies only at the person's request (resume_verification.verify_confident). People,
companies and resumes here are invented."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.services import structured_resume as sr
from app.services import worker_settings
from app.services.resume_extraction.confidence import assess
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)
TEXT = """Alex Rivera
alex@example.test
Skills
Kafka, K8s, Python
Experience
Lead Engineer | Acme | Remote | Jan 2020 - Dec 2024
- Built the event platform on Kafka for 40 teams.
"""


def data(**over):
    base = {"personal_information": {"full_name": "Alex Rivera", "email": "alex@example.test"},
            "skills": [{"name": "Apache Kafka", "raw": "Kafka"}, {"name": "Kubernetes"}, {"name": "Python"},
                       {"name": "Rust"}],
            "experience": [{"job_title": "Lead Engineer", "company": "Acme", "location": "Remote",
                            "start_date": "2020-01", "end_date": "2024-12", "raw_start_date": "Jan 2020",
                            "raw_end_date": "Dec 2024",
                            "achievements": [{"text": "Built the event platform on Kafka for 40 teams."},
                                             {"text": "Built the event platform on Kafka for 41 teams."}]}]}
    return sr.materialize(sr.StructuredResumeIn.model_validate({**base, **over}))


def row(route="clean", corrections=()):
    return SimpleNamespace(quality={"route": route}, corrections=list(corrections), layout=None)


def levels(structured, text=TEXT, record=None):
    out = assess(structured, text, record or row())
    by = {}
    for unit in sr.iter_units(structured):
        key = unit.node.get("name") or unit.node.get("text") or unit.node.get("job_title") or unit.node.get("full_name")
        by[key] = out[unit.item_id]
    return by


def test_a_fact_the_text_states_word_for_word_is_high_and_the_rest_says_why():
    by = levels(data())
    assert by["Python"]["level"] == "high" and by["Python"]["reasons"] == ["in the file's text word for word"]
    assert by["Lead Engineer"]["level"] == "high", "normalized dates are not words of the text"
    assert by["Alex Rivera"]["level"] == "high"
    assert by["Apache Kafka"] == {"level": "medium", "reasons": ["normalized from “Kafka”"]}
    assert by["Kubernetes"] == {"level": "medium", "reasons": ["found through a known alias"]}
    assert by["Rust"]["level"] == "low" and "not in the text as written" in by["Rust"]["reasons"][0]
    assert by["Built the event platform on Kafka for 41 teams."]["level"] == "low", "a number is never fuzzy"


def test_a_corrected_word_lowers_a_fact_unless_the_page_and_a_rule_agree_or_the_person_kept_it():
    text = TEXT.replace("Kafka", "Kaga")
    fix = {"id": "c1", "original": "Kaga", "corrected": "Kafka", "method": "ocr", "count": 2, "status": "applied"}
    bullet = "Built the event platform on Kafka for 40 teams."
    assert levels(data(), text, row("suspicious", [fix]))[bullet]["level"] == "medium"
    assert levels(data(), text, row("suspicious", [{**fix, "method": "ocr+rule"}]))[bullet]["level"] == "high"
    assert levels(data(), text, row("suspicious", [{**fix, "status": "kept"}]))[bullet]["level"] == "high"
    undone = levels(data(), text, row("suspicious", [{**fix, "status": "undone"}]))[bullet]
    assert undone["level"] == "low", "the file's own word does not match the fact"


def test_a_poor_file_lowers_every_fact_one_level():
    by = levels(data(), record=row("poor"))
    assert by["Python"]["level"] == "medium" and "the file's text was poor" in by["Python"]["reasons"]


def test_a_misread_looking_word_is_low_in_a_flagged_file():
    text = TEXT.replace("Built", "BuilY")
    by = levels(data(experience=[{"job_title": "Lead Engineer", "company": "Acme",
                                  "achievements": [{"text": "BuilY the event platform on Kafka for 40 teams."}]}]),
                text, row("suspicious"))
    assert by["BuilY the event platform on Kafka for 40 teams."]["level"] == "low"


# ───────────── through the screen ─────────────

@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    return user_id


def _upload(user_id, v2: bool) -> dict:
    db = SessionLocal()
    db.info["user_id"] = user_id
    worker_settings.update(db, user_id, {"resume_parsing": {"extraction_v2": v2}})
    db.close()
    out = client.post("/api/profile/resume", files={"file": ("cv.txt", io.BytesIO(TEXT.encode()), "text/plain")})
    return client.get(f"/api/settings/resume?resume_id={out.json()['resume_id']}").json()["current"]


def test_accept_high_confidence_verifies_only_unverified_high_facts(user):
    detail = _upload(user, v2=True)
    high = [i for i, c in detail["confidence"].items() if c["level"] == "high"]
    assert high and set(detail["confidence"]) >= set(high)
    edited = detail["structured"]["skills"][0]["id"]
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": edited, "status": "REJECTED"})
    out = client.post(f"/api/resumes/{detail['id']}/verify-confident").json()
    assert out["verified"] == len([i for i in high if i != edited])
    statuses = {u.item_id: u.status for u in sr.iter_units(out["structured"])}
    assert statuses[edited] == "REJECTED", "a fact the person decided is left alone"
    assert all(statuses[i] == "VERIFIED" for i in high if i != edited)
    assert client.post(f"/api/resumes/{detail['id']}/verify-confident").json()["verified"] == 0


def test_a_version_read_without_v2_has_no_confidence(user):
    detail = _upload(user, v2=False)
    assert detail["confidence"] == {}
    assert client.post(f"/api/resumes/{detail['id']}/verify-confident").json()["verified"] == 0
