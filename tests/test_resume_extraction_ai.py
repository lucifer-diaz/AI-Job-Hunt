"""Resume extraction V2, Phase 5: the AI parser reads the parse view (prompt v7), a normalized skill keeps how the
resume writes it, and what grounding leaves out is held back for the person instead of vanishing. People, companies
and resumes here are invented; the model is a scripted answer."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.models import Resume, ResumeExtraction, ResumeExtractionFeedback
from app.prompts import resume_parser as prompts
from app.services import resume_parsing as rp
from app.services import structured_resume as sr
from app.services import worker_settings
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

RESUME = """Alex Rivera
Platform Engineer
alex@example.test | +1 555 0100

Summary
Platform engineer who builds internal tools.

Skills
Kafka, K8s, GCP, Python

Experience
Lead Engineer | Acme | Remote | Jan 2020 - Dec 2024
- Built the event platform on Kafka for 40 teams.
"""


def answer() -> dict:
    """What a model might return: normalized skills, one skill and one bullet the resume does not state."""
    return sr.from_parser_output({
        "personal_information": {"full_name": "Alex Rivera", "headline": "Platform Engineer",
                                 "email": "alex@example.test"},
        "summary": "Platform engineer who builds internal tools.",
        "skills": [{"name": "Apache Kafka", "raw": "Kafka", "category": "Skills"},
                   {"name": "Kubernetes", "raw": "K8s", "category": "Skills"},
                   {"name": "Google Cloud Platform", "raw": "GCP", "category": "Skills"},
                   {"name": "Python", "raw": None, "category": "Skills"},
                   {"name": "Rust", "raw": "Python", "category": "Skills"}],
        "experience": [{"company": "Acme", "job_title": "Lead Engineer", "location": "Remote",
                        "raw_start_date": "Jan 2020", "raw_end_date": "Dec 2024",
                        "achievements": ["Built the event platform on Kafka for 40 teams.",
                                         "Cut cloud spend by 30% across all regions."]}],
    })


# ───────────── rules ─────────────

@pytest.mark.parametrize(("raw", "name", "ok"), [
    ("Kafka", "Apache Kafka", True), ("K8s", "Kubernetes", True), ("GCP", "Google Cloud Platform", True),
    ("Postgres", "PostgreSQL", True), ("Python", "Rust", False), ("Go", "Google", False), ("AI", "Airflow", False),
    ("", "Kafka", False)])
def test_a_normalized_name_must_be_checkably_the_same_skill(raw, name, ok):
    assert rp.normalizes(raw, name) is ok


def test_grounding_keeps_a_normalized_skill_with_its_written_form_and_holds_back_the_rest():
    data = answer()
    grounded, dropped = rp.ground(data, RESUME)
    skills = {s["name"]: s.get("raw") for s in grounded["skills"]}
    assert skills == {"Apache Kafka": "Kafka", "Kubernetes": "K8s", "Google Cloud Platform": "GCP", "Python": None}
    assert dropped["skills"] == 1 and dropped["bullets"] == 1
    held = rp.held_back(data, grounded)
    assert [(h["kind"], h["value"], h["where"]) for h in held] == [
        ("skill", "Rust", "Skills"), ("bullet", "Cut cloud spend by 30% across all regions.", "Lead Engineer · Acme")]
    assert len({h["id"] for h in held}) == 2


def test_materialized_skills_carry_raw_only_when_they_have_one():
    data = sr.materialize(sr.StructuredResumeIn.model_validate(
        {"skills": [{"name": "Apache Kafka", "raw": "Kafka"}, {"name": "Go"}]}))
    assert [set(s) for s in data["skills"]] == [{"id", "name", "category", "raw", "verification"},
                                                {"id", "name", "category", "verification"}]


def test_v2_resumes_get_the_v7_prompt_and_everyone_else_keeps_v6():
    old, new = prompts.build_resume_parser_prompt("text"), prompts.build_resume_parser_prompt("text", marked=True)
    assert (old.version, new.version) == ("v6", "v7")
    assert "Structure:" in new.system and '"raw":null' in new.system and "Ka[ka -> Kafka" not in new.system
    assert "Structure:" not in old.system and "Ka[ka -> Kafka" in old.system and '"raw"' not in old.system


# ───────────── through the worker and the screen ─────────────

@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    monkeypatch.setattr(get_settings(), "resume_parse_enabled", True)
    with _db(user_id) as db:
        worker_settings.update(db, user_id, {"resume_parsing": {"extraction_v2": True}})
    return user_id


def _db(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def _sessions(user_id):
    return lambda: _db(user_id)


def _claim(user_id) -> rp.ParseClaim:
    with _db(user_id) as db:
        return next(c for c in rp.claim(db, worker_id="test", limit=50) if c.user_id == user_id)


def test_the_ai_parse_reads_the_view_and_holds_back_what_the_text_lacks(user):
    out = client.post("/api/profile/resume", files={"file": ("cv.txt", io.BytesIO(RESUME.encode()), "text/plain")})
    resume_id = out.json()["resume_id"]
    seen = {}

    def generate(prompt, call):
        seen["version"], seen["user"] = prompt.version, prompt.user
        return answer()

    assert rp.process(_sessions(user), _claim(user), generate) == rp.FILLED
    assert seen["version"] == "v7" and "## Skills" in seen["user"], "the model read the parse view"
    with _db(user) as db:
        resume = db.get(Resume, resume_id)
        assert resume.parser_version == "v7"
        assert {s["name"]: s.get("raw") for s in resume.structured_data["skills"]}["Apache Kafka"] == "Kafka"
        row = db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == resume_id))
        assert [i["value"] for i in row.review] == ["Rust", "Cut cloud spend by 30% across all regions."]
    detail = client.get(f"/api/settings/resume?resume_id={resume_id}").json()["current"]
    review = {i["value"]: i for i in detail["extraction"]["review"]}
    assert review["Rust"]["addable"] and review["Cut cloud spend by 30% across all regions."]["addable"]

    added = client.post(f"/api/resumes/{resume_id}/review", json={"id": review["Rust"]["id"], "action": "add"})
    assert added.status_code == 200, added.text
    rust = next(s for s in added.json()["structured"]["skills"] if s["name"] == "Rust")
    assert rust["verification"]["status"] == "UNVERIFIED", "the person's addition is reviewed like any other fact"
    bullet = review["Cut cloud spend by 30% across all regions."]["id"]
    added = client.post(f"/api/resumes/{resume_id}/review", json={"id": bullet, "action": "add"}).json()
    texts = [b["text"] for b in added["structured"]["experience"][0]["achievements"]]
    assert "Cut cloud spend by 30% across all regions." in texts
    assert added["extraction"]["review"] == []
    assert client.post(f"/api/resumes/{resume_id}/review", json={"id": bullet, "action": "add"}).status_code == 400
    with _db(user) as db:
        actions = db.scalars(select(ResumeExtractionFeedback.after).where(ResumeExtractionFeedback.user_id == user,
                                                                          ResumeExtractionFeedback.kind == "value")).all()
        assert actions == [{"action": "add"}, {"action": "add"}]


def test_a_resume_read_without_v2_is_parsed_as_before(user):
    with _db(user) as db:
        worker_settings.update(db, user, {"resume_parsing": {"extraction_v2": False}})
    client.post("/api/profile/resume", files={"file": ("cv.txt", io.BytesIO(RESUME.encode()), "text/plain")})
    seen = {}

    def generate(prompt, call):
        seen["version"], seen["user"] = prompt.version, prompt.user
        return answer()

    assert rp.process(_sessions(user), _claim(user), generate) == rp.FILLED
    assert seen["version"] == "v6" and "## Skills" not in seen["user"]
