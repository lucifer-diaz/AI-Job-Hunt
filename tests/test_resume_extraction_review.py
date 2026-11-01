"""Resume extraction V2, Phase 4: a person assigns the headings the reader found (store.assign), the account learns
its own headings (store.learned), and a heading nobody could place blocks verification until it is assigned.
People, companies and resumes here are invented."""
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
from app.services import worker_settings
from app.services.resume_extraction import sections, store
from app.services.resume_extraction.models import LineInfo
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)

# "Selected Engagements" is written like the other headings but no table knows it: it starts its own section.
RESUME = """Alex Rivera
Platform Engineer
alex@example.test | +1 555 0100

SUMMARY
Platform engineer who builds internal tools.

SELECTED ENGAGEMENTS
Kafka, Terraform, Go

EXPERIENCE
Lead Engineer | Acme | Remote | Jan 2020 - Dec 2024
- Built the event platform.

EDUCATION
B.S. Computer Science | State University | 2012 - 2016
"""


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    with _db(user_id) as db:
        worker_settings.update(db, user_id, {"resume_parsing": {"extraction_v2": True}})
    return user_id


def _db(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def _upload(text: str = RESUME, name: str = "cv.txt") -> dict:
    out = client.post("/api/profile/resume", files={"file": (name, io.BytesIO(text.encode()), "text/plain")})
    assert out.status_code == 200, out.text
    return client.get(f"/api/settings/resume?resume_id={out.json()['resume_id']}").json()["current"]


def _heading(detail: dict, text: str) -> dict:
    return next(s for s in detail["extraction"]["sections"] if s["text"] == text)


def test_a_text_file_keeps_its_unknown_heading_out_of_its_neighbours():
    infos = [LineInfo(text=t) for t in RESUME.split("\n")]
    # Without layout a heading the table does not know is not a heading: its lines stay where they are written.
    assert [h.text for h in sections.detect(infos)] == ["SUMMARY", "EXPERIENCE", "EDUCATION"]


def test_assigning_a_heading_re_reads_the_resume_and_is_learnt(user):
    detail = _upload()
    assert detail["extraction"]["unplaced"] == 0  # text: the unknown heading is not one yet
    line = RESUME.split("\n").index("SELECTED ENGAGEMENTS")
    out = client.post(f"/api/resumes/{detail['id']}/sections", json={"line": line, "kind": "skills"})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["section_result"] == {"filled": True}  # nobody had reviewed it: the data is filled again
    assert {s["name"] for s in body["structured"]["skills"]} == {"Kafka", "Terraform", "Go"}
    assert _heading(body, "SELECTED ENGAGEMENTS")["method"] == "person"
    assert "Kafka" not in body["structured"]["summary"]["text"]
    with _db(user) as db:
        assert store.learned(db, user) == {"selected engagements": "skills"}
        assert db.scalar(select(ResumeExtractionFeedback.subject).where(ResumeExtractionFeedback.user_id == user)) \
            == "SELECTED ENGAGEMENTS"
    # The next resume with that heading is read with the person's choice, before any table.
    second = _upload(RESUME.replace("Alex Rivera", "Alex R. Rivera"), name="cv2.txt")
    assert _heading(second, "SELECTED ENGAGEMENTS")["method"] == "learned"
    assert {"Kafka", "Terraform", "Go"} <= {s["name"] for s in second["structured"]["skills"]}


def test_not_a_section_puts_its_lines_back_where_they_are_written(user):
    detail = _upload()
    line = RESUME.split("\n").index("EDUCATION")
    body = client.post(f"/api/resumes/{detail['id']}/sections", json={"line": line, "kind": "none"}).json()
    assert "EDUCATION" not in [s["text"] for s in body["extraction"]["sections"]]
    assert body["structured"]["education"] == []


def test_a_reviewed_resume_gets_the_new_reading_as_a_comparison(user):
    detail = _upload()
    client.post(f"/api/resumes/{detail['id']}/verify-item", json={"item_id": detail["structured"]["summary"]["id"],
                                                                 "status": "VERIFIED"})
    line = RESUME.split("\n").index("SELECTED ENGAGEMENTS")
    body = client.post(f"/api/resumes/{detail['id']}/sections", json={"line": line, "kind": "skills"}).json()
    assert body["section_result"] == {"filled": False}
    assert body["pending_reparse"] and body["structured"]["skills"] == [], "nothing changes until the person chooses"


def test_an_unplaced_heading_blocks_verification_until_it_is_assigned(user):
    detail = _upload()
    with _db(user) as db:  # as a PDF in the house style would have it: a heading the table cannot place
        row = db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == detail["id"]))
        line = RESUME.split("\n").index("SELECTED ENGAGEMENTS")
        row.sections = sorted([*row.sections, {"line": line, "text": "SELECTED ENGAGEMENTS", "kind": "other",
                                               "method": "unknown", "signal": "layout"}], key=lambda s: s["line"])
        db.commit()
        resume = db.get(Resume, detail["id"])
        client.post(f"/api/resumes/{detail['id']}/verify-section", json={"section": "summary"})
    assert client.get(f"/api/settings/resume?resume_id={detail['id']}").json()["current"]["extraction"]["unplaced"] == 1
    refused = client.post(f"/api/resumes/{detail['id']}/verify")
    assert refused.status_code == 422
    assert refused.json()["detail"]["blocking"][0]["label"].startswith("Heading “SELECTED ENGAGEMENTS”")
    client.post(f"/api/resumes/{detail['id']}/sections", json={"line": line, "kind": "additional_information"})
    with _db(user) as db:
        assert store.unplaced(db, detail["id"]) == []
    assert resume.id == detail["id"]


def test_a_choice_must_be_a_section_and_a_line_of_the_resume(user):
    detail = _upload()
    assert client.post(f"/api/resumes/{detail['id']}/sections", json={"line": 4, "kind": "hobbies"}).status_code == 400
    assert client.post(f"/api/resumes/{detail['id']}/sections", json={"line": 999, "kind": "skills"}).status_code == 400
    assert client.post(f"/api/resumes/{detail['id']}/sections", json={"line": 3, "kind": "skills"}).status_code == 400


def test_learning_counts_choices_and_the_latest_wins_a_tie(user):
    with _db(user) as db:
        for kind in ("skills", "projects", "projects", "skills"):
            db.add(ResumeExtractionFeedback(user_id=user, kind="section", subject="Toolbox", after={"kind": kind}))
        db.add(ResumeExtractionFeedback(user_id=user, kind="section", subject="Side Quests", after={"kind": "projects"}))
        db.commit()
        assert store.learned(db, user) == {"toolbox": "skills", "side quests": "projects"}
    other = make_user(SessionLocal)
    with _db(other) as db:
        assert store.learned(db, other) == {}, "never another account's choices"
