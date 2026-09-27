"""A posting with no description is not ranked, and is ranked later if a description turns up.

Several boards — Naukri and Bayt in particular — return listings with nothing but a title, a company and a
link. Ranking those means asking a model to score a job title: it answers confidently about nothing, spends a
call and often fails outright. They wait instead.
"""
import uuid

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Job
from app.services import ranker, sources
from app.services.lifecycle import WorkflowState as S
from app.services.ranker import RankingPolicy
from app.settings import get_settings
from tests.conftest import ensure_test_user

REAL_POSTING = "We are hiring a lead engineer. " * 20


@pytest.fixture
def owner():
    return ensure_test_user().id


@pytest.fixture(autouse=True)
def clean(owner):
    from app.models import WorkflowTransition

    with SessionLocal() as db:
        ids = select(Job.id).where(Job.user_id == owner, Job.source.in_(("apify:naukri", "apify:bayt", "manual")))
        # SQLite does not enforce the cascade: without this a reused job id inherits another job's history.
        db.execute(delete(WorkflowTransition).where(WorkflowTransition.job_id.in_(ids)))
        db.execute(delete(Job).where(Job.id.in_(ids)))
        db.commit()
    yield


def make_job(owner, description, *, source="apify:naukri", structured=None, state=S.DISCOVERED):
    with SessionLocal() as db:
        job = Job(user_id=owner, source=source, source_ref=uuid.uuid4().hex, title="Associate Product Manager",
                  company="Kapture Crm", description=description, structured_description=structured, state=state)
        db.add(job)
        db.commit()
        return job.id


def prepare(job_id):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        reason = ranker.prepare(db, job, RankingPolicy(minimum_fit_score=70, minimum_confidence=0.6),
                                actor="test")
        db.commit()
        db.refresh(job)
        return reason, job.state


# ───────────── what counts as a description ─────────────

def test_a_posting_without_one_is_not_rankable(owner):
    assert ranker.missing_description(Job(description="")) is True
    assert ranker.missing_description(Job(description=None)) is True
    assert ranker.missing_description(Job(description="Apply now on our careers site")) is True
    assert ranker.missing_description(Job(description=REAL_POSTING)) is False


def test_a_parsed_description_counts_even_when_the_raw_text_is_short(owner):
    """The parser only produces structure from real text, so structure is proof there was some."""
    assert ranker.missing_description(Job(description="short", structured_description={"requirements": [1]})) is False


def test_the_floor_is_configurable(owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "ranking_min_description_chars", 10)
    assert ranker.missing_description(Job(description="x" * 20)) is False
    monkeypatch.setattr(get_settings(), "ranking_min_description_chars", 500)
    assert ranker.missing_description(Job(description="x" * 20)) is True


def test_the_normalized_text_is_used_when_there_is_one(owner):
    job = Job(description="stub", normalized_description=REAL_POSTING)
    assert ranker.missing_description(job) is False


# ───────────── what the ranker does with it ─────────────

def test_a_description_less_posting_never_reaches_the_queue(owner):
    reason, state = prepare(make_job(owner, ""))
    assert reason == ranker.NO_DESCRIPTION
    assert state is S.SKIPPED, "it is not queued, so no model call is spent on it"


def test_a_real_posting_passes_as_before(owner):
    reason, state = prepare(make_job(owner, REAL_POSTING))
    assert reason is None and state is S.FILTERED


def test_the_skip_says_why(owner):
    from app.models import WorkflowTransition

    job_id = make_job(owner, "")
    prepare(job_id)
    with SessionLocal() as db:
        moved = db.scalar(select(WorkflowTransition).where(WorkflowTransition.job_id == job_id,
                                                           WorkflowTransition.to_state == S.SKIPPED))
    assert moved.reason == ranker.NO_DESCRIPTION


def test_it_is_ranked_after_all_once_a_description_arrives(owner):
    job_id = make_job(owner, "")
    prepare(job_id)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.description = REAL_POSTING
        assert ranker.reopen_for_description(db, job) is True
        db.commit()
        db.refresh(job)
        assert job.state is S.NORMALIZED
    reason, state = prepare(job_id)
    assert reason is None and state is S.FILTERED


def test_a_re_import_with_a_fuller_posting_reopens_it(owner):
    """The usual way a description turns up: the board is searched again and returns more this time."""
    with SessionLocal() as db:
        db.info["user_id"] = owner
        job, _ = sources.upsert_job(db, source="apify:naukri", source_ref="reopen-me", title="Associate PM",
                                    company="Kapture Crm", description="")
        job_id = job.id
    prepare(job_id)
    with SessionLocal() as db:
        assert db.get(Job, job_id).state is S.SKIPPED
        db.info["user_id"] = owner
        sources.upsert_job(db, source="apify:naukri", source_ref="reopen-me", title="Associate PM",
                           company="Kapture Crm", description=REAL_POSTING)
    with SessionLocal() as db:
        assert db.get(Job, job_id).state is S.NORMALIZED, "the fuller posting put it back in the queue"


def test_a_skip_for_any_other_reason_stays_skipped(owner):
    """Only the missing-description skip is reversible; a job skipped for want of a title is not."""
    job_id = make_job(owner, REAL_POSTING)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.title = ""
        db.commit()
    reason, state = prepare(job_id)
    assert reason == "Missing title or company" and state is S.SKIPPED
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.title, job.description = "Associate PM", REAL_POSTING
        assert ranker.reopen_for_description(db, job) is False, "that skip was about the title, not the text"


def test_reopening_needs_a_description_that_is_actually_there(owner):
    job_id = make_job(owner, "")
    prepare(job_id)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.description = "still nothing much"
        assert ranker.reopen_for_description(db, job) is False


def test_the_tracker_counts_what_is_waiting_on_a_description(owner):
    for _ in range(3):
        prepare(make_job(owner, ""))
    prepare(make_job(owner, REAL_POSTING))
    with SessionLocal() as db:
        counts = ranker.tracker(db, owner)["counts"]
    assert counts["no_description"] == 3


# ───────────── re-ranking is held back too ─────────────

def test_a_ranked_job_without_a_description_is_not_re_ranked(owner):
    """prepare() only guards a first ranking; a re-rank request must not score a title either."""
    job_id = make_job(owner, "", state=S.RANKED)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        why = ranker.queue_job(db, job, policy=RankingPolicy(minimum_fit_score=70, minimum_confidence=0.6),
                               actor="test", reason="user_request", force=True)
        db.commit()
        db.refresh(job)
        assert why == ranker.NO_DESCRIPTION
        assert job.state is S.RANKED, "the job keeps its state; nothing was queued"
