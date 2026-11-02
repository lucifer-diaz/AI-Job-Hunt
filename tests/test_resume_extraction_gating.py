"""Resume extraction V2, downstream gating: with unverified facts allowed, generation and review use an unverified
fact of a V2 resume only when its confidence is high (context_builder.hold_back_unconfident). Ranking is not gated,
a person's own facts are kept, and a version read without V2 is used as before. People and companies are invented."""
from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.auth import CurrentUser
from app.db import SessionLocal
from app.main import app
from app.mcp import tools
from app.models import Job, Resume
from app.prompts import build_review_prompt
from app.services import context_builder as cb
from app.services import resume_verification as rv
from app.services import structured_resume as sr
from app.services import worker_settings
from app.services.user_settings import profile_for
from app.settings import get_settings
from tests.conftest import make_user

client = TestClient(app)
TEXT = """Alex Rivera
alex@example.test
Skills
Kafka, Python
Experience
Lead Engineer | Acme | Remote | Jan 2020 - Dec 2024
- Built the event platform on Kafka for 40 teams.
- Ran the on-call rotation for the Kafka platform.
"""
LOW = "Ran the on-call rotation for the Kafka platform."


@pytest.fixture
def user(monkeypatch):
    user_id = make_user(SessionLocal)
    monkeypatch.setattr(auth, "authenticate", lambda request: CurrentUser(user_id, f"{user_id}@jobhunt.test", False))
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    return user_id


def session(user_id):
    db = SessionLocal()
    db.info["user_id"] = user_id
    return db


def upload(user_id, v2=True) -> int:
    with session(user_id) as db:
        worker_settings.update(db, user_id, {"resume_parsing": {"extraction_v2": v2}})
    out = client.post("/api/profile/resume", files={"file": ("cv.txt", io.BytesIO(TEXT.encode()), "text/plain")})
    return out.json()["resume_id"]


def add_job(user_id) -> int:
    with session(user_id) as db:
        job = Job(source="manual", source_ref=f"gate-{uuid.uuid4().hex[:8]}", title="Platform Lead", company="Globex",
                  description="Own the Kafka event platform and the on-call rotation. Python.", status="shortlisted")
        db.add(job)
        db.commit()
        return job.id


def rate_low(monkeypatch, text=LOW):
    """The bullet `text` is read with low confidence; everything else as assessed."""
    real = rv.confidence

    def confidence(db, resume):
        levels = dict(real(db, resume))
        for unit in sr.iter_units(resume.structured_data):
            if unit.node.get("text") == text:
                levels[unit.item_id] = {"level": "low", "reasons": ["“on-call” is not in the text as written"]}
        return levels

    monkeypatch.setattr(rv, "confidence", confidence)


def texts(context):
    shown = [b["text"] for role in context.relevant_experience for b in role["bullets"]]
    return shown + [f["text"] for f in context.unverified_facts]


def test_an_unverified_fact_below_high_is_held_back_from_review_and_the_reviewer_is_told(user, monkeypatch):
    upload(user)
    rate_low(monkeypatch)
    job_id = add_job(user)
    with session(user) as db:
        context = cb.build_review_context(db, profile_for(db), db.get(Job, job_id))
        assert LOW not in texts(context) and "Built the event platform on Kafka for 40 teams." in texts(context)
        held = {f["text"]: f for f in context.held_back_facts}
        assert held[LOW]["level"] == "low" and held[LOW]["reasons"]
        assert "held_back_facts" not in context.selected_payload(), "metadata, not counted as prompt context"
        prompt = build_review_prompt(candidate_profile=profile_for(db), job=db.get(Job, job_id), context=context,
                                     tailored_resume="# Alex")
        assert "HELD-BACK RESUME FACTS" in prompt.user and LOW in prompt.user
        stored = db.scalar(sr_resume(user))
        assert all(u.status != cb.HELD_BACK for u in sr.iter_units(stored.structured_data)), "the stored data is untouched"


def sr_resume(user_id):
    from sqlalchemy import select

    return select(Resume).where(Resume.user_id == user_id, Resume.is_active.is_(True))


def test_nothing_held_back_leaves_the_review_prompt_as_it_was(user):
    upload(user)  # every fact of this file is in its text word for word: high
    job_id = add_job(user)
    with session(user) as db:
        context = cb.build_review_context(db, profile_for(db), db.get(Job, job_id))
        assert context.held_back_facts == [] and LOW in texts(context)
        prompt = build_review_prompt(candidate_profile=profile_for(db), job=db.get(Job, job_id), context=context)
        assert "HELD-BACK" not in prompt.user


def test_a_held_role_holds_its_bullets_and_a_persons_own_facts_are_kept(user, monkeypatch):
    rid = upload(user)
    with session(user) as db:
        resume = db.get(Resume, rid)
        role = resume.structured_data["experience"][0]
        levels = {role["id"]: {"level": "medium", "reasons": ["normalized"]}}
        monkeypatch.setattr(rv, "confidence", lambda db, r: levels)
        data, held = cb.hold_back_unconfident(db, resume, resume.structured_data, cb.WITH_UNVERIFIED, "tailoring")
        by_text = {f["text"]: f for f in held}
        assert by_text[LOW]["reasons"] == ["its role is held back"]
        copy = data["experience"][0]
        assert {u.status for u in sr.iter_units(data) if u.parent is copy or u.node is copy} == {cb.HELD_BACK}
        assert all(u.status != cb.HELD_BACK for u in sr.iter_units(data) if u.section == "skills")
        # Verified-only, ranking, or a fact the person already decided: nothing is held back.
        assert cb.hold_back_unconfident(db, resume, resume.structured_data, cb.VERIFIED_ONLY, "tailoring")[1] == []
        assert cb.hold_back_unconfident(db, resume, resume.structured_data, cb.WITH_UNVERIFIED, "ranking")[1] == []
        edited = {**resume.structured_data,
                  "experience": [{**role, "verification": {"status": "EDITED"}, "achievements": [],
                                  "responsibilities": []}]}
        assert cb.hold_back_unconfident(db, resume, edited, cb.WITH_UNVERIFIED, "tailoring")[1] == []


def test_a_version_read_without_v2_is_used_as_before(user, monkeypatch):
    upload(user, v2=False)
    job_id = add_job(user)
    with session(user) as db:
        context = cb.build_review_context(db, profile_for(db), db.get(Job, job_id))
        assert context.held_back_facts == [] and LOW in texts(context)


def test_mcp_tailoring_and_review_context_name_what_is_held_back(user, monkeypatch):
    upload(user)
    rate_low(monkeypatch)
    with session(user) as db:
        view = rv.selected(db, profile_for(db))
        out = tools._held_back(db, view)
        assert [f["text"] for f in out["held_back_facts"]] == [LOW] and out["held_back_note"]
