"""Job Insights API: an approved job's researched package, its sources, a refresh, and the person's own inputs.

Reads are projections of stored research (app.services.job_insights.views); nothing here fetches a page, runs an actor
or calls a model. Refresh only queues a task (deduplicated: one active task per job) and nudges the scheduler. Every
endpoint works in the signed-in account's session, so another account's job is simply not found.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import Application, Evidence, Job, JobInsight
from app.scheduler_hooks import run_soon
from app.services import knowledge
from app.services.job_insights import matching, service, views
from app.services.job_insights import tasks as insight_tasks
from app.services.url_safety import UnsafeURL, check_public_url

router = APIRouter()
STAR = ("Situation: the context, in one or two sentences", "Task: what you were responsible for",
        "Action: what you did yourself, with the decisions you made",
        "Result: the outcome, with a number where the evidence states one")


class RecruiterIn(BaseModel):
    recruiter_name: str = Field(default="", max_length=200)
    budget_low: float | None = Field(default=None, gt=0, lt=100_000_000)
    budget_high: float | None = Field(default=None, gt=0, lt=100_000_000)
    currency: str | None = Field(default=None, max_length=10)
    period: Literal["year", "month", "week", "day", "hour"] | None = None
    basis: Literal["BASE", "TOTAL_CASH", "TOTAL_COMPENSATION", "UNKNOWN"] | None = None
    interview_process: str = Field(default="", max_length=2000)
    recruiter_questions: list[str] = Field(default_factory=list, max_length=30)
    comments: str = Field(default="", max_length=2000)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        value = (value or "").strip().upper()
        if value and (len(value) != 3 or not value.isalpha()):
            raise ValueError("Use a three-letter currency code, e.g. SGD")
        return value or None


class InputsIn(BaseModel):
    # None leaves it as it is; "" clears the website the person entered (the posting's own is used again).
    company_website: str | None = Field(default=None, max_length=500)
    recruiter: RecruiterIn | None = None


def _job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/api/jobs/{job_id}/insights")
def job_insights(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """The job's insights with the state of its research (queued, running, last success, last failure)."""
    return views.insight_view(db, _job(db, job_id))


@router.post("/api/jobs/{job_id}/insights/refresh")
def refresh(job_id: int, request: Request, background: BackgroundTasks,
            db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Find insights (first time) or Refresh: research again now, past the cache. A refresh while one is queued or
    running returns that one; previous insights stay visible until the new run succeeds."""
    job = _job(db, job_id)
    has_insight = db.scalar(select(JobInsight.refreshed_at).where(JobInsight.job_id == job.id)) is not None
    reason = insight_tasks.REFRESH if has_insight else insight_tasks.USER_REQUEST
    try:
        task = insight_tasks.enqueue(db, job, reason=reason, force=True)
        db.commit()
    except IntegrityError:  # another request queued it at the same moment
        db.rollback()
        task = insight_tasks.active_task(db, job.id)
    run_soon(background, request.app, "job_insights")
    return {"queued": task is not None, "task": views.task_view(task), **views.insight_view(db, job)}


@router.get("/api/jobs/{job_id}/insights/compensation")
def compensation(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    full = views.insight_view(db, _job(db, job_id))
    return {"job_id": job_id, "compensation": full.get("compensation"), "prep": full.get("compensation_prep"),
            "status": full["status"], "refreshed_at": full.get("refreshed_at")}


@router.get("/api/jobs/{job_id}/insights/company")
def company(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    full = views.insight_view(db, _job(db, job_id))
    return {"job_id": job_id, "company": full.get("company"), "identity": full.get("identity"),
            "risks": full.get("risks"), "unknowns": full.get("unknowns"), "refreshed_at": full.get("refreshed_at")}


@router.get("/api/jobs/{job_id}/insights/interview")
def interview(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    full = views.insight_view(db, _job(db, job_id))
    return {"job_id": job_id, "interview": full.get("interview"), "questions_to_ask": full.get("questions_to_ask"),
            "refreshed_at": full.get("refreshed_at")}


@router.get("/api/jobs/{job_id}/insights/sources")
def sources(job_id: int, offset: int = Query(0, ge=0), limit: int = Query(views.PAGE_SIZE, ge=1, le=views.PAGE_SIZE),
            db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Provenance: the observations behind the insights (paged; `next_offset` when there are more), and what each
    source returned on the last run."""
    return views.observations_view(db, _job(db, job_id), offset=offset, limit=limit)


@router.put("/api/jobs/{job_id}/insights/inputs")
def save_inputs(job_id: int, payload: InputsIn, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """The person's own facts: the company's website, and what the recruiter said. Research never overwrites them;
    they are used from the next research run (Refresh)."""
    job = _job(db, job_id)
    insight = service.ensure_insight(db, job)
    if payload.company_website is not None:
        site = payload.company_website.strip()
        if site:
            url = site if "://" in site else f"https://{site}"
            try:
                check_public_url(url)
            except UnsafeURL as exc:
                raise HTTPException(422, str(exc)) from exc
            insight.company_website = f"https://{matching.domain_of(url)}"
            insight.company_website_source = "USER"
        else:
            insight.company_website_source = None
            website = service.posting_website(job.raw)
            insight.company_website, insight.company_website_source = website, "POSTING" if website else None
        insight.company_domain = matching.domain_of(insight.company_website)
    if payload.recruiter is not None:
        recruiter = payload.recruiter.model_dump()
        recruiter["recruiter_questions"] = [q.strip()[:300] for q in recruiter["recruiter_questions"] if q.strip()]
        if recruiter["budget_low"] and recruiter["budget_high"] and recruiter["budget_low"] > recruiter["budget_high"]:
            raise HTTPException(422, "The budget's low end is above its high end")
        insight.recruiter_input = {k: v for k, v in recruiter.items() if v not in (None, "", [])}
    db.commit()
    return views.insight_view(db, job)


@router.get("/api/applications/{application_id}/insights")
def application_insights(application_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """The job's insights adapted to the application's tracker stage (recruiter screen, technical, offer, ...)."""
    application = db.get(Application, application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    return views.stage_view(db, application)


@router.post("/api/jobs/{job_id}/insights/questions/{question_id}/prepare")
def prepare(job_id: int, question_id: str, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """An answer outline for one generated question: STAR structure, the verified evidence to draw on, and the gaps
    not to claim. Built from stored evidence only (no model call), and never saved as evidence."""
    job = _job(db, job_id)
    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
    question = next((q for q in (insight.generated_questions if insight else None) or []
                     if q.get("id") == question_id), None)
    if question is None:
        raise HTTPException(404, "Question not found")
    cited = list(dict.fromkeys(question.get("evidence_ids") or []))
    rows = db.scalars(select(Evidence).where(Evidence.id.in_(cited), Evidence.verified.is_(True))).all() if cited \
        else []
    evidence = [{"id": r.id, "claim": r.claim, "role": r.role, "company": r.company, "source": "cited"}
                for r in sorted(rows, key=lambda r: cited.index(r.id))]
    found = knowledge.search(db, question["question"], limit=5)["evidence"]
    evidence += [{"id": e["id"], "claim": e["claim"], "role": e.get("role"), "company": e.get("company"),
                  "source": "related"} for e in found if e["id"] not in {x["id"] for x in evidence}]
    return {"question": question["question"], "origin": question.get("origin"), "kind": "GENERATED",
            "outline": list(STAR), "evidence": evidence[:8],
            "gaps_not_to_claim": question.get("gaps") or [], "preparation": question.get("preparation"),
            "note": "A preparation outline from your verified evidence. It is not saved, and it is not evidence."}
