from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Application, ATSQuestion, CandidateProfile, Job, now
from app.services.applications import (
    create_answer_version,
    inspect_application_questions,
    normalize_question,
    resolve_occurrence,
    semantic_confidence,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def application(db: Session, ref: str) -> Application:
    job = Job(source="test", source_ref=ref, title="Engineer", company="Example")
    result = Application(job=job)
    db.add(result)
    db.commit()
    return result


def observed(text="What is your favorite programming language?", **changes):
    item = {
        "external_key": "q1", "text": text, "category": "experience",
        "answer_type": "text", "allowed_choices": [], "sensitivity": "none",
    }
    item.update(changes)
    return item


def test_normalization_duplicate_and_materially_different_wording(db):
    assert normalize_question("  WHAT is your name? (Required) ") == "what is your name"
    first = inspect_application_questions(db, application(db, "a"), [observed()])[0]
    duplicate = inspect_application_questions(
        db, application(db, "b"), [observed("WHAT is your favorite programming language? *")]
    )[0]
    different = inspect_application_questions(
        db, application(db, "c"), [observed("What is your least favorite programming language?")]
    )[0]
    assert duplicate.question_id == first.question_id
    assert duplicate.match_kind == "exact"
    assert different.question_id != first.question_id
    assert semantic_confidence(first.raw_question_text, different.raw_question_text) < 0.92


def test_verified_answer_reuse_has_confidence_and_provenance(db):
    first = inspect_application_questions(db, application(db, "a"), [observed()])[0]
    answer = resolve_occurrence(db, first, "Python")
    reused = inspect_application_questions(
        db, application(db, "b"), [observed("What is your favorite programming language?")]
    )[0]
    assert reused.resolution_state == "reused"
    assert reused.answer_id == answer.id
    assert reused.match_confidence == 1.0
    assert reused.provenance == {
        "canonical_question_id": first.question_id,
        "answer_version_id": answer.id,
    }


@pytest.mark.parametrize("state", ["unverified", "rejected"])
def test_unverified_answers_are_rejected_for_reuse(db, state):
    first = inspect_application_questions(db, application(db, "a"), [observed()])[0]
    create_answer_version(db, first.question, "Python", state)
    db.commit()
    second = inspect_application_questions(db, application(db, "b"), [observed()])[0]
    assert second.resolution_state == "needs_approval"
    assert second.answer_id is None


def test_answer_type_mismatch_and_stale_answers_require_approval(db):
    first = inspect_application_questions(db, application(db, "a"), [observed()])[0]
    create_answer_version(db, first.question, "Python", "verified", expires_at=now() - timedelta(days=1))
    db.commit()
    stale = inspect_application_questions(db, application(db, "b"), [observed()])[0]
    mismatch = inspect_application_questions(
        db, application(db, "c"), [observed(answer_type="boolean")]
    )[0]
    assert stale.resolution_state == "needs_approval"
    assert mismatch.resolution_state == "needs_approval"
    assert "type differs" in mismatch.conflict_warning.lower()


def test_conflicting_human_answer_creates_auditable_version(db):
    first = inspect_application_questions(db, application(db, "a"), [observed()])[0]
    resolve_occurrence(db, first, "Python")
    second = inspect_application_questions(db, application(db, "b"), [observed()])[0]
    changed = resolve_occurrence(db, second, "Rust")
    assert changed.version == 2
    assert "conflicts" in second.conflict_warning
    assert second.provenance["source_application_id"] == second.application_id


def test_sensitive_answer_requires_narrow_policy_opt_in(db):
    item = observed(
        "Will you now or in the future require visa sponsorship?",
        category="work_authorization", answer_type="boolean",
    )
    first = inspect_application_questions(db, application(db, "a"), [item])[0]
    resolve_occurrence(db, first, False)
    blocked = inspect_application_questions(db, application(db, "b"), [item])[0]
    assert blocked.resolution_state == "needs_approval"

    db.add(CandidateProfile(id=1, search_preferences={
        "ats_auto_answer_policies": {"work_authorization": {"enabled": True, "scope": "visa sponsorship"}}
    }))
    db.commit()
    allowed = inspect_application_questions(db, application(db, "c"), [item])[0]
    assert allowed.resolution_state == "reused"


def test_choice_mismatch_is_not_reused(db):
    item = observed(answer_type="single_choice", allowed_choices=["Yes", "No"])
    first = inspect_application_questions(db, application(db, "a"), [item])[0]
    resolve_occurrence(db, first, "Yes")
    changed = inspect_application_questions(
        db, application(db, "b"), [observed(answer_type="single_choice", allowed_choices=["Y", "N"])]
    )[0]
    assert changed.resolution_state == "needs_approval"
    assert "choices differ" in changed.conflict_warning.lower()


def test_invalid_answer_type_is_rejected(db):
    question = ATSQuestion(
        normalized_text="years experience", fingerprint="fingerprint", category="experience",
        answer_type="number", allowed_choices=[], sensitivity="none",
    )
    db.add(question)
    with pytest.raises(ValueError, match="not compatible"):
        create_answer_version(db, question, "five", "verified")
