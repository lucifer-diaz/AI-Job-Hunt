"""Helpers for the resume preparation graph tests: a qualified job, the mock model, and the passes that move it.

`drive` runs the preparation scheduler pass and the tailor worker until nothing changes, so a job ends where the system
would leave it: waiting for a person, or decided. `script_reviews` replaces the Reviewer's answer (the model the tailor
worker calls) with a list of recommendations, one per tailoring pass; `inject_claim` makes every tailoring pass save a
bullet no evidence supports, as a model that invents would.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.models import (
    AgentThread,
    Job,
    JobPreparationTask,
    JobRankingTask,
    Resume,
    ResumeVariant,
    TailorTask,
)
from app.services import scheduled_jobs as sj
from app.services import tailor_tasks, workflow
from app.settings import get_settings
from tests.conftest import COMPLETED_FIT_ANALYSIS
from tests.test_ranker import POSTING, RESUME, add_owner

INVENTED = "Grew revenue by 73% by leading a team of 40 engineers across 9 countries"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def configure(monkeypatch, mode: str = "on", max_revisions: int = 2) -> None:
    settings = get_settings()
    for name, value in (("agent_graphs_enabled", True), ("preparation_graph_mode", mode),
                        ("preparation_max_revisions", max_revisions), ("llm_provider", "mock"),
                        ("rag_retrieval_mode", "off"), ("rag_vector_enabled", False),
                        ("job_insights_enabled", True)):
        monkeypatch.setattr(settings, name, value)


def set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(get_settings(), "preparation_graph_mode", mode)


def owner(sessions) -> int:
    """An account with a profile and its active resume version (as the first upload records it)."""
    from app.models import CandidateProfile
    from app.services import resume_verification as rv

    user_id = add_owner(sessions, resume=RESUME)
    with sessions() as db:
        db.info["user_id"] = user_id
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
        rv.active_resume(db, profile)
        db.commit()
    return user_id


def qualified_job(sessions, user_id: int, *, ranked: bool = True, **fields: Any) -> int:
    """A job the Ranker qualified: QUALIFIED, in the Tailor queue, with its completed fit analysis."""
    from app.services.lifecycle import WorkflowState as S

    with sessions() as db:
        db.info["user_id"] = user_id
        job = Job(user_id=user_id, source="test", source_ref=uuid.uuid4().hex, title="Engineering Manager",
                  company="Globex", description=POSTING, url=f"https://jobs.example.com/{uuid.uuid4().hex}",
                  status="shortlisted", state=S.QUALIFIED, fit_score=82 if ranked else None,
                  fit_analysis=dict(COMPLETED_FIT_ANALYSIS) if ranked else None, **fields)
        db.add(job)
        db.commit()
        return job.id


def dispatch(sessions, user_id: int, job_id: int) -> int:
    from app.services import preparation

    with sessions() as db:
        db.info["user_id"] = user_id
        task = preparation.dispatch(db, db.get(Job, job_id), reason="test")
        db.commit()
        return task.id


def ctx(sessions) -> sj.JobContext:
    return sj.JobContext(sessions=sessions, trigger="schedule", now=utcnow(), worker_id=f"test:{uuid.uuid4().hex[:6]}",
                         interval_minutes=1)


def _snapshot(sessions) -> tuple:
    with sessions() as db:
        rows = [("prep", t.id, t.status, t.waiting_for, t.step) for t in db.scalars(select(JobPreparationTask))]
        rows += [("tailor", t.id, t.status, "", "") for t in db.scalars(select(TailorTask))]
        rows += [("variant", v.id, str(v.reviewer_score), "", "") for v in db.scalars(select(ResumeVariant))]
        return tuple(sorted(rows, key=str))


def drive(sessions, rounds: int = 16) -> int:
    """The preparation pass and the tailor worker, until a whole round changes nothing."""
    for done in range(1, rounds + 1):
        before = _snapshot(sessions)
        asyncio.run(sj.preparation_job(ctx(sessions)))
        tailor_tasks.run_pending(sessions, worker_id="test-tailor")
        with sessions() as db:
            from sqlalchemy import update

            db.execute(update(JobPreparationTask).where(JobPreparationTask.status == "PENDING")
                       .values(next_attempt_at=None))
            db.commit()
        if _snapshot(sessions) == before:
            return done
    raise AssertionError("the passes did not settle")


def script_reviews(monkeypatch, recommendations: list[str], score: float = 88) -> list[int]:
    """The Reviewer answers each pass with the next recommendation (the last one repeats). Returns the versions it
    reviewed, in order."""
    seen: list[int] = []

    def review(db, resume):
        rec = recommendations[min(len(seen), len(recommendations) - 1)]
        seen.append(resume.version)
        notes = {"review_score": score, "score": score, "confidence": 0.9, "recommendation": rec,
                 "unsupported_claims": ["A claim the evidence does not support"] if rec == "RETAILOR" else [],
                 "overstated_claims": [], "missing_requirements": [], "risks": [], "source": "backend",
                 "summary": "scripted"}
        workflow.store_review(db, resume, notes)
        return notes

    monkeypatch.setattr(workflow, "review_resume", review)
    clean_letters(monkeypatch)
    return seen


def clean_letters(monkeypatch) -> None:
    """The mock cover letter writes a number no fact states (the evidence checks rightly send that back). Tests that
    need a packet to pass use a letter that only says what the resume says."""
    def letter(db, profile, job, llm, tailored=None):
        return "I lead Python and FastAPI platform teams.", {"validation_issues": [], "evidence_ids": []}

    monkeypatch.setattr(workflow, "write_cover_letter", letter)


def inject_claim(monkeypatch) -> None:
    """Every tailoring pass saves one extra highlight that cites a fact that does not exist (this resume has no
    parsed roles, so tailored bullets are highlights)."""
    original = workflow.persist_tailored_resume

    def persist(db, job, content, *, markdown=None):
        content = {**content, "bullet_evidence": [*(content.get("bullet_evidence") or []),
                                                  {"role_index": None, "text": INVENTED, "evidence_ids": [987654]}],
                   "experience_bullets": [*(content.get("experience_bullets") or []), INVENTED]}
        return original(db, job, content, markdown=markdown)

    monkeypatch.setattr(workflow, "persist_tailored_resume", persist)


def task_of(sessions, job_id: int, mode: str = "ON") -> JobPreparationTask:
    with sessions() as db:
        row = db.scalar(select(JobPreparationTask).where(JobPreparationTask.job_id == job_id,
                                                         JobPreparationTask.mode == mode)
                        .order_by(JobPreparationTask.id.desc()).limit(1))
        db.expunge(row)
        return row


def thread_of(sessions, task: JobPreparationTask) -> AgentThread | None:
    with sessions() as db:
        row = db.get(AgentThread, task.agent_thread_id) if task.agent_thread_id else None
        if row is not None:
            db.expunge(row)
        return row


def counts(sessions, job_id: int) -> dict[str, int]:
    with sessions() as db:
        return {name: db.scalar(select(func.count()).select_from(m).where(m.job_id == job_id)) or 0
                for name, m in (("tailor", TailorTask), ("variants", ResumeVariant), ("rankings", JobRankingTask))}


def variants(sessions, job_id: int) -> list[ResumeVariant]:
    with sessions() as db:
        rows = db.scalars(select(ResumeVariant).where(ResumeVariant.job_id == job_id)
                          .order_by(ResumeVariant.version)).all()
        for r in rows:
            db.expunge(r)
        return list(rows)


def resumes(sessions, user_id: int) -> list[tuple[int, int, str | None, str | None]]:
    """Every source resume of the account as it is stored: id, version, raw text hash and status."""
    with sessions() as db:
        return [(r.id, r.version, r.raw_text_hash, getattr(r, "verification_status", None))
                for r in db.scalars(select(Resume).where(Resume.user_id == user_id).order_by(Resume.id))]


# ───────────── the merge-checkpoint parity report (tests and scripts/preparation_parity.py) ─────────────

class Patcher:
    """monkeypatch.setattr for the script: set attributes and undo them all afterwards."""

    def __init__(self) -> None:
        self._undo: list[tuple[Any, str, Any]] = []

    def setattr(self, target: Any, name: str, value: Any) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        while self._undo:
            target, name, value = self._undo.pop()
            setattr(target, name, value)


# (name, reviewer recommendations per pass, score, invent a claim, keep the mock letter)
CASES = (
    ("pass", ["PASS"], 88, False, False),
    ("reviewer sends back, then passes", ["RETAILOR", "PASS"], 88, False, False),
    ("reviewer always sends back", ["RETAILOR"], 88, False, False),
    ("score below the minimum", ["PASS"], 40, False, False),
    ("reviewer says skip", ["SKIP"], 88, False, False),
    ("invented claim, reviewer passes it", ["PASS"], 88, True, False),
    ("letter with an invented number", ["PASS"], 88, False, True),
)
UNIT_FIELDS = ("summary", "skills", "experience", "experience_bullets", "bullet_evidence", "selected_evidence_ids")


def _fact_issues(sessions, user_id: int, job_id: int, variant_id: int) -> int:
    from app.agents.preparation import FACT_CODES
    from app.services import resume_verification as rv
    from app.services.tailoring_results import from_variant_content, validate_tailored_resume

    with sessions() as db:
        db.info["user_id"] = user_id
        job, variant = db.get(Job, job_id), db.get(ResumeVariant, variant_id)
        data, legacy = from_variant_content(job_id, variant.content or {})
        issues = validate_tailored_resume(db, rv.for_job(db, workflow.get_profile(db), job), job, data, legacy=legacy)
        return sum(1 for i in issues if i["code"] in FACT_CODES)


def _legacy(sessions, user_id: int, job_id: int) -> None:
    """The current path: the tailor worker, then the review decision Claude Desktop makes (review_results)."""
    from app.services import review_results

    with sessions() as db:
        db.info["user_id"] = user_id
        tailor_tasks.enqueue(db, [job_id])
    tailor_tasks.run_pending(sessions, worker_id="legacy")
    with sessions() as db:
        db.info["user_id"] = user_id
        job, variant = db.get(Job, job_id), review_results.latest_variant(db, job_id)
        review = review_results.review_of(job_id, variant)
        if review is not None:
            review_results.apply_review(db, job, variant, review, source="current_path")


def run_case(sessions, patch: Any, case: tuple) -> dict[str, Any]:
    name, reviews, score, invent, mock_letter = case
    patcher = Patcher() if patch is None else None
    setter = patch or patcher
    try:
        if invent:
            inject_claim(setter)
        out: dict[str, Any] = {"case": name}
        for path in ("current", "graph"):
            script_reviews(setter, reviews, score=score)  # each path gets the same script from its first pass
            if mock_letter:
                setter.setattr(workflow, "write_cover_letter", _ORIGINAL_LETTER)
            set_mode(setter, "off" if path == "current" else "on")
            user = owner(sessions)
            before = resumes(sessions, user)
            job_id = qualified_job(sessions, user)
            if path == "current":
                _legacy(sessions, user, job_id)
            else:
                dispatch(sessions, user, job_id)
                drive(sessions)
            rows = variants(sessions, job_id)
            first = rows[0]
            out[path] = {
                "first_units": {k: (first.content or {}).get(k) for k in UNIT_FIELDS},
                "first_letter": first.cover_letter, "first_decision": (first.reviewer_notes or {}).get("decision"),
                "versions": len(rows), "status": _job_status(sessions, job_id),
                "source_resume_unchanged": resumes(sessions, user) == before,
                "passed_with_unsupported": any((v.reviewer_notes or {}).get("decision") == "PASS"
                                               and _fact_issues(sessions, user, job_id, v.id) for v in rows),
                "rankings": counts(sessions, job_id)["rankings"]}
        current, graph = out["current"], out["graph"]
        out["same_tailoring"] = current["first_units"] == graph["first_units"] and \
            current["first_letter"] == graph["first_letter"]
        out["same_first_decision"] = current["first_decision"] == graph["first_decision"]
        return out
    finally:
        if patcher is not None:
            patcher.undo()


def _job_status(sessions, job_id: int) -> str:
    with sessions() as db:
        return db.get(Job, job_id).status


_ORIGINAL_LETTER = workflow.write_cover_letter


def report(sessions, patch: Any = None) -> list[dict[str, Any]]:
    return [run_case(sessions, patch, case) for case in CASES]
