"""Phase 4 evaluations (app.services.preparation_eval) over the hand-labelled dataset, on mock providers.

The evidence cases run the real deterministic checks (tailoring_results.validate_tailored_resume) in a throwaway
account with one verified fact; the other cases run the graph's own rules, the provenance check and the compensation
rules. Every case must score 1.0: a failure names the case and what the code produced.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.db import SessionLocal
from app.models import CandidateProfile, Evidence, Job
from app.services import preparation_eval
from app.services.lifecycle import WorkflowState as S
from tests.conftest import COMPLETED_FIT_ANALYSIS, make_user
from tests.test_ranker import POSTING, RESUME

GROUNDED = "Led a team of eight engineers building Python and FastAPI platforms"


@pytest.fixture(scope="module")
def claims():
    """validate(inputs) → issue codes, in an account whose only verified fact is GROUNDED."""
    user = make_user(SessionLocal)
    with SessionLocal() as db:
        db.info["user_id"] = user
        db.add(CandidateProfile(user_id=user, name="Asha Rao", headline="Engineering Manager",
                                target_roles=["Engineering Manager"], master_resume=RESUME,
                                work_authorization="Singapore PR"))
        fact = Evidence(user_id=user, company="Acme", role="Engineering manager", claim=GROUNDED,
                        skills=["Python", "FastAPI"], verified=True)
        job = Job(user_id=user, source="test", source_ref="eval", title="Engineering Manager", company="Globex",
                  description=POSTING, state=S.QUALIFIED, status="shortlisted", fit_analysis=COMPLETED_FIT_ANALYSIS)
        db.add_all([fact, job])
        db.commit()
        fact_id, job_id = fact.id, job.id

    def validate(inputs: dict[str, Any]) -> list[str]:
        from app.services import resume_verification as rv
        from app.services.tailoring_results import (
            BulletIn,
            TailoredResumeIn,
            validate_tailored_resume,
        )
        from app.services.workflow import get_profile

        aliases = {"lead": [fact_id], "missing": [987654], None: []}
        bullets = [BulletIn.model_construct(text=b["text"], evidence_ids=aliases[b.get("evidence")])
                   for b in inputs.get("bullets") or []]
        with SessionLocal() as db:
            db.info["user_id"] = user
            job = db.get(Job, job_id)
            data = TailoredResumeIn.model_construct(job_id=job_id, summary="", skills_order=inputs.get("skills") or [],
                                                    experience_sections=[], resume_bullets=bullets, cover_letter="",
                                                    screening_answer_drafts={})
            issues = validate_tailored_resume(db, rv.for_job(db, get_profile(db), job), job, data)
        return sorted({i["code"] for i in issues})

    return validate


def test_the_dataset_covers_every_evaluator():
    examples = preparation_eval.load()
    assert {e.evaluator for e in examples} == set(preparation_eval.EVALUATORS)
    assert len({e.key for e in examples}) == len(examples)
    for example in examples:  # LangSmith's Example shape
        assert set(example.langsmith()) == {"inputs", "outputs", "metadata"}


@pytest.mark.parametrize("example", preparation_eval.load(), ids=lambda e: e.key)
def test_every_case_scores_one(example, claims):
    (result,) = preparation_eval.run([example], claims=claims)
    assert result["score"] == 1.0, f"{example.key}: {result['comment'] or result['outputs']}"


def test_the_summary_counts_every_case(claims):
    results = preparation_eval.run(preparation_eval.load(), claims=claims)
    summary = preparation_eval.summary(results)
    assert summary["examples"] == summary["passed"] == len(preparation_eval.load())
    assert set(summary["by_evaluator"]) == set(preparation_eval.EVALUATORS)
