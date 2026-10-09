"""What the Job Insights API returns: the stored package, the run state, the sources table and the stage view.

Read-only projections over job_insights, insight_observations and job_insight_tasks, always in the signed-in account's
session (app.tenancy). Nothing here calls a provider or a model. Error text is redacted, review text is never
returned (only aggregates and the themes derived from it), and no credential is ever part of a payload.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, InsightObservation, Job, JobInsight, JobInsightTask
from app.services import redaction
from app.services.job_insights import service
from app.services.job_insights.base import (
    BLOCKED,
    FAILED,
    NEEDS_REVIEW,
    NO_DATA,
    NOT_CONFIGURED,
    OK,
)
from app.services.job_insights.tasks import active_task, latest_task

SYMBOLS = {OK: "✓", NO_DATA: "—", FAILED: "!", BLOCKED: "!", NOT_CONFIGURED: "○", NEEDS_REVIEW: "?",
           "NOT_RUN": "·"}
SYMBOL_LEGEND = {"✓": "available", "—": "no data", "!": "failed", "○": "not configured", "?": "needs review",
                 "·": "not researched yet"}
# Tracker stage → the preparation that matters next (app.services.tracker stages).
STAGE_FOCUS = {
    "approved": ("RECRUITER_SCREEN",), "applied": ("RECRUITER_SCREEN",), "acknowledged": ("RECRUITER_SCREEN",),
    "referral": ("RECRUITER_SCREEN",), "screening": ("RECRUITER_SCREEN",),
    "interviewing": ("HIRING_MANAGER", "TECHNICAL", "SYSTEM_DESIGN", "BEHAVIORAL", "FINAL"),
    "takehome": ("TECHNICAL", "CODING"), "offer": ("HR_OFFER",), "negotiating": ("HR_OFFER",),
}
FOCUS_LABELS = {
    "RECRUITER_SCREEN": ("Recruiter screen", ("Company summary, salary, motivation, visa and notice period, and "
                                              "questions for the recruiter")),
    "HIRING_MANAGER": ("Hiring manager", "Role mandate, leadership, impact and stakeholder questions"),
    "TECHNICAL": ("Technical", "Skills, coding and architecture questions"),
    "CODING": ("Coding", "Hands-on problems in the posting's stack"),
    "SYSTEM_DESIGN": ("System design", "Architecture themes, scalability and reliability scenarios"),
    "BEHAVIORAL": ("Behavioral", "Values, leadership, conflict, failure and ownership"),
    "FINAL": ("Final round", "Company strategy, leadership, culture and questions for executives"),
    "HR_OFFER": ("HR / offer", "Compensation, benefits, level, equity and negotiation questions"),
}
# Which questions-to-ask stages and generated-question stages each focus surfaces.
ASK_FOR = {"RECRUITER_SCREEN": ("RECRUITER",), "HIRING_MANAGER": ("HIRING_MANAGER",),
           "TECHNICAL": ("TECHNICAL_INTERVIEWER",), "CODING": ("TECHNICAL_INTERVIEWER",),
           "SYSTEM_DESIGN": ("TECHNICAL_INTERVIEWER", "ENGINEERING_LEADER"), "BEHAVIORAL": ("HIRING_MANAGER",),
           "FINAL": ("ENGINEERING_LEADER", "FINAL_HR"), "HR_OFFER": ("FINAL_HR", "RECRUITER")}
QUESTION_STAGES = {"RECRUITER_SCREEN": ("RECRUITER_SCREEN",), "HIRING_MANAGER": ("HIRING_MANAGER",),
                   "TECHNICAL": ("TECHNICAL", "CODING"), "CODING": ("CODING", "TECHNICAL"),
                   "SYSTEM_DESIGN": ("SYSTEM_DESIGN",), "BEHAVIORAL": ("BEHAVIORAL",), "FINAL": ("FINAL",),
                   "HR_OFFER": ("HR_OFFER",)}
# Observation fields safe to show as provenance. Review text (CULTURE) and page excerpts never leave the server.
PUBLIC_FIELDS = {"COMPENSATION": ("low", "high", "median", "currency", "period", "basis", "sample_size"),
                 "VALUES": ("value", "heading"), "COMPANY": ("field", "text"),
                 "SENTIMENT": ("overall_rating", "career_opportunities", "compensation_rating", "work_life_balance",
                               "management", "culture", "sample_size"),
                 "INTERVIEW_PROCESS": ("difficulty", "duration", "outcome"), "INTERVIEW_QUESTION": ("question", "stage"),
                 "CULTURE": ()}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _redact(text: str | None) -> str | None:
    return redaction.redact_text(text)[:600] if text else None


def task_view(task: JobInsightTask | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {"id": task.id, "status": task.status, "reason": task.reason, "attempts": task.attempt_count,
            "force_refresh": task.force_refresh, "created_at": _iso(task.created_at),
            "started_at": _iso(task.started_at), "completed_at": _iso(task.completed_at),
            "next_attempt_at": _iso(task.next_attempt_at), "last_error": _redact(task.last_error),
            "active": task.status in ("PENDING", "PROCESSING")}


def sources_table(task: JobInsightTask | None) -> list[dict[str, Any]]:
    """Every source in priority order, with what the last run got from it (or why it could not run)."""
    ran = (task.source_status or {}) if task is not None else {}
    rows = []
    for source in service.SOURCES:
        state = ran.get(source.name)
        if state is None:
            why = source.configured()
            state = {"status": NOT_CONFIGURED if why else "NOT_RUN", "detail": why}
        status = state.get("status") or "NOT_RUN"
        rows.append({"name": source.name, "label": source.label, "source_type": source.source_type,
                     "status": status, "symbol": SYMBOLS.get(status, "·"), "detail": _redact(state.get("detail")),
                     "observations": state.get("observations", 0), "cached": bool(state.get("cached")),
                     "rejected": state.get("rejected", 0), "needs_review": state.get("needs_review", 0),
                     "fetched_at": state.get("fetched_at")})
    return rows


def _finished_task(db: Session, job_id: int) -> JobInsightTask | None:
    return db.scalar(select(JobInsightTask).where(JobInsightTask.job_id == job_id,
                                                  JobInsightTask.status.not_in(("PENDING", "PROCESSING")))
                     .order_by(JobInsightTask.id.desc()).limit(1))


def overview(insight: JobInsight | None) -> dict[str, Any]:
    if insight is None:
        return {}
    comp = insight.compensation or {}
    company = insight.company or {}
    questions = insight.generated_questions or []
    process = insight.interview_process or {}
    reported_rounds = (process.get("estimated_rounds") or {}).get("value")
    return {"compensation": {"status": comp.get("status"), "confidence": comp.get("confidence"),
                             "range": comp.get("recommended_discussion_range")},
            "interview": {"reported_rounds": reported_rounds, "stages": len(process.get("stages") or []),
                          "reported_questions": len(insight.reported_questions or []),
                          "role_derived_questions": sum(1 for q in questions if q.get("origin") == "ROLE_DERIVED"),
                          "candidate_specific_questions": sum(1 for q in questions
                                                              if q.get("origin") == "CANDIDATE_SPECIFIC"),
                          "focus_areas": [f.get("area") for f in insight.focus_areas or []][:8]},
            "company": {"summary": (company.get("summary") or {}).get("text"),
                        "official_values": len(company.get("official_values") or []),
                        "culture_themes": len(company.get("culture_themes") or [])}}


def insight_view(db: Session, job: Job) -> dict[str, Any]:
    """The whole package for one job, with the state of its research."""
    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
    active, latest = active_task(db, job.id), latest_task(db, job.id)
    finished = _finished_task(db, job.id)
    application = db.scalar(select(Application).where(Application.job_id == job.id))
    refresh_failed = bool(insight and insight.last_attempt_at and insight.last_error and finished is not None
                          and finished.status == "FAILED")
    base = {"job": {"id": job.id, "title": job.title, "company": job.company, "location": job.location,
                    "application_id": application.id if application else None,
                    "application_status": application.status if application else None,
                    "stage": application.stage if application and application.status == "applied" else None},
            "eligible": bool(application and application.status in ("approved", "applied")),
            "status": insight.status if insight else "NONE", "task": task_view(active or latest),
            "researching": active is not None, "refresh_failed": refresh_failed,
            "last_successful_at": _iso(insight.refreshed_at) if insight else None,
            "sources": sources_table(finished),
            "legend": SYMBOL_LEGEND, "kinds": {"FACT": "Stated by a source", "DERIVED": "Interpreted from sources",
                                               "GENERATED": "Preparation written by a model",
                                               "UNKNOWN": "Not known"}}
    if insight is None:
        return {**base, "identity": None, "overview": {}, "compensation": None, "compensation_prep": None,
                "company": None, "interview": None, "questions_to_ask": [], "risks": [], "unknowns": [],
                "recruiter_input": None}
    generated = insight.generated_questions or []
    return {**base,
            "generated_at": _iso(insight.generated_at), "refreshed_at": _iso(insight.refreshed_at),
            "last_attempt_at": _iso(insight.last_attempt_at), "last_error": _redact(insight.last_error),
            "model": insight.model, "prompt_versions": insight.prompt_versions, "confidence": insight.confidence,
            "identity": {"company_name": insight.company_name, "canonical_company": insight.canonical_company,
                         "website": insight.company_website, "website_source": insight.company_website_source,
                         "domain": insight.company_domain, "original_title": insight.original_title,
                         "normalized_title": insight.normalized_title, "seniority": insight.seniority,
                         "discipline": insight.discipline, "city": insight.city, "country": insight.country},
            "overview": overview(insight),
            "compensation": insight.compensation, "compensation_prep": insight.compensation_prep,
            "company": insight.company,
            "interview": {"process": insight.interview_process, "reported_questions": insight.reported_questions or [],
                          "role_derived_questions": [q for q in generated if q.get("origin") == "ROLE_DERIVED"],
                          "candidate_specific_questions": [q for q in generated
                                                           if q.get("origin") == "CANDIDATE_SPECIFIC"],
                          "focus_areas": insight.focus_areas or [], "value_prep": insight.value_prep or []},
            "questions_to_ask": insight.questions_to_ask or [], "risks": insight.risks or [],
            "unknowns": insight.unknowns or [], "recruiter_input": insight.recruiter_input}


def public_data(row: InsightObservation) -> dict[str, Any]:
    data = row.data or {}
    fields = PUBLIC_FIELDS.get(row.category, ())
    out = {k: data.get(k) for k in fields if data.get(k) not in (None, "")}
    if row.category == "COMPANY" and data.get("field") == "page_extract":
        out.pop("text", None)  # a page excerpt is model input, not something to republish
    if isinstance(out.get("text"), str):
        out["text"] = out["text"][:300]
    return out


PAGE_SIZE = 200


def observations_view(db: Session, job: Job, *, offset: int = 0, limit: int = PAGE_SIZE) -> dict[str, Any]:
    """Provenance for every researched fact: where it came from, when, and how well it matched this job. Paged: the
    total and the next offset say when there is more. Superseded rows still cited by a kept section are included."""
    insight = db.scalar(select(JobInsight).where(JobInsight.job_id == job.id))
    limit, offset = max(1, min(limit, PAGE_SIZE)), max(0, offset)
    total = 0 if insight is None else db.scalar(select(func.count(InsightObservation.id)).where(
        InsightObservation.job_insight_id == insight.id)) or 0
    rows = [] if insight is None else db.scalars(select(InsightObservation).where(
        InsightObservation.job_insight_id == insight.id).order_by(InsightObservation.id)
        .offset(offset).limit(limit)).all()
    following = offset + len(rows)
    return {"job_id": job.id, "total": total, "offset": offset, "truncated": following < total,
            "next_offset": following if following < total else None, "observations": [
        {"id": r.id, "category": r.category, "source_type": r.source_type, "source_name": r.source_name,
         "provider": r.provider, "actor_id": r.actor_id, "source_url": r.source_url,
         "source_reference": r.source_reference, "title": r.title, "match_scope": r.match_scope,
         "match_status": r.match_status, "match_reason": r.match_reason, "confidence": r.confidence,
         "recency": r.recency, "observed_at": _iso(r.observed_at), "fetched_at": _iso(r.fetched_at),
         "superseded": r.superseded_at is not None, "data": public_data(r)} for r in rows],
        "sources": sources_table(_finished_task(db, job.id))}


def stage_view(db: Session, application: Application) -> dict[str, Any]:
    """The job's insights, adapted to where the application is now. A projection: nothing is copied or stored."""
    job = db.get(Job, application.job_id)
    full = insight_view(db, job)
    key = application.stage if application.status == "applied" else application.status
    focus = STAGE_FOCUS.get(key or "", ())
    asks = {s for f in focus for s in ASK_FOR.get(f, ())}
    q_stages = {s for f in focus for s in QUESTION_STAGES.get(f, ())}
    interview = full.get("interview") or {}
    generated = interview.get("role_derived_questions", []) + interview.get("candidate_specific_questions", [])
    return {"application_id": application.id, "job_id": application.job_id, "status": application.status,
            "stage": application.stage if application.status == "applied" else None,
            "focus": [{"id": f, "label": FOCUS_LABELS[f][0], "covers": FOCUS_LABELS[f][1]} for f in focus],
            "questions_to_ask": [q for q in full.get("questions_to_ask") or [] if q.get("stage") in asks],
            "likely_questions": [q for q in generated if q.get("stage") in q_stages],
            "value_prep": interview.get("value_prep", []) if "BEHAVIORAL" in focus else [],
            "compensation_prep": full.get("compensation_prep") if {"RECRUITER_SCREEN", "HR_OFFER"} & set(focus)
            else None,
            "company_summary": ((full.get("company") or {}).get("summary") if "RECRUITER_SCREEN" in focus
                                or "FINAL" in focus else None),
            "insights": full}
