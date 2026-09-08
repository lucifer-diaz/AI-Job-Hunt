from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db
from app.models import (ATSQuestion, ATSQuestionOccurrence, Application, CandidateProfile,
                        Evidence, FollowUp, Job, ResumeVariant, SearchRun)
from app.schemas import (ApifySearchIn, ATSAnswerIn, ATSInspectionIn, ATSQuestionIn,
                         ATSQuestionUpdate, ATSResolutionIn, EvidenceIn, ManualJobIn, ProfileIn,
                         RecruiterIn)
from app.services.applications import (create_answer_version, inspect_application_questions,
                                       normalize_question, question_fingerprint, resolve_occurrence,
                                       semantic_confidence)
from app.services.exports import markdown_to_simple_pdf
from app.services.sources import import_greenhouse, import_lever, refresh_apify_run, start_apify_search, upsert_job
from app.services.workflow import approve_job, create_due_followups, get_profile, mark_applied, rank_job, tailor_job
from app.settings import get_settings

Base.metadata.create_all(bind=engine)
settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.3.0")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    # Leave health checks public. If username/password are unset, auth is disabled for local development.
    if request.url.path == "/health" or not (settings.app_username and settings.app_password):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    authenticated = False
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            username, password = decoded.split(":", 1)
            authenticated = secrets.compare_digest(username, settings.app_username) and secrets.compare_digest(password, settings.app_password)
        except Exception:
            authenticated = False
    if not authenticated:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Job Hunt OS"'})
    return await call_next(request)


templates = Jinja2Templates(directory="app/templates")


def job_dict(j: Job) -> dict:
    return {
        "id": j.id, "title": j.title, "company": j.company, "location": j.location,
        "url": j.url, "source": j.source, "fit_score": j.fit_score,
        "fit_analysis": j.fit_analysis, "status": j.status, "discovered_at": j.discovered_at.isoformat(),
    }


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(UTC).isoformat()}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    jobs = db.scalars(select(Job).order_by(desc(Job.discovered_at)).limit(100)).all()
    profile = get_profile(db)
    evidence = db.scalars(select(Evidence).order_by(desc(Evidence.id)).limit(50)).all()
    applications = db.scalars(select(Application).order_by(desc(Application.id)).limit(50)).all()
    followups = db.scalars(select(FollowUp).order_by(desc(FollowUp.id)).limit(50)).all()
    search_runs = db.scalars(select(SearchRun).order_by(desc(SearchRun.id)).limit(10)).all()
    knowledge = db.scalars(select(ATSQuestion).order_by(desc(ATSQuestion.updated_at)).limit(100)).all()
    pending_questions = db.scalars(select(ATSQuestionOccurrence).where(
        ATSQuestionOccurrence.resolution_state == "needs_approval"
    ).order_by(desc(ATSQuestionOccurrence.created_at)).limit(100)).all()
    stats = {
        "jobs": db.scalar(select(func.count(Job.id))) or 0,
        "shortlisted": db.scalar(select(func.count(Job.id)).where(Job.status == "shortlisted")) or 0,
        "tailored": db.scalar(select(func.count(Job.id)).where(Job.status == "tailored")) or 0,
        "applied": db.scalar(select(func.count(Job.id)).where(Job.status == "applied")) or 0,
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context={ "jobs": jobs, "profile": profile, "evidence": evidence,
        "applications": applications, "followups": followups, "search_runs": search_runs, "stats": stats,
        "llm_provider": settings.llm_provider, "apify_actor_id": settings.apify_actor_id,
        "knowledge": knowledge,
        "pending_questions": pending_questions,
    })


@app.get("/api/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(desc(Job.discovered_at))).all()]


@app.post("/api/jobs/manual")
def add_job(payload: ManualJobIn, db: Session = Depends(get_db)):
    ref = hashlib.sha256(f"{payload.company}|{payload.title}|{payload.url}".encode()).hexdigest()[:40]
    job, created = upsert_job(db, source="manual", source_ref=ref, title=payload.title,
                              company=payload.company, location=payload.location, url=payload.url,
                              description=payload.description)
    return {"created": created, "job": job_dict(job)}


@app.post("/api/import/lever/{site}")
def lever(site: str, db: Session = Depends(get_db)):
    try:
        return import_lever(db, site)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/import/greenhouse/{board_token}")
def greenhouse(board_token: str, db: Session = Depends(get_db)):
    try:
        return import_greenhouse(db, board_token)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/search/apify")
def apify_search(payload: ApifySearchIn, db: Session = Depends(get_db)):
    try:
        run = start_apify_search(db, payload.model_dump())
        return {
            "id": run.id,
            "external_run_id": run.external_run_id,
            "actor_id": run.actor_id,
            "status": run.status,
        }
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/search/apify/runs/{run_id}")
def apify_run_status(run_id: int, db: Session = Depends(get_db)):
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    try:
        return refresh_apify_run(db, run)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/search/apify/runs")
def apify_runs(db: Session = Depends(get_db)):
    runs = db.scalars(select(SearchRun).order_by(desc(SearchRun.id)).limit(50)).all()
    return [
        {
            "id": r.id, "external_run_id": r.external_run_id, "actor_id": r.actor_id,
            "status": r.status, "item_count": r.item_count, "created_jobs": r.created_jobs,
            "created_at": r.created_at.isoformat(), "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "error": r.error,
        }
        for r in runs
    ]


@app.post("/api/jobs/{job_id}/rank")
def rank(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        return rank_job(db, job)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/rank-batch")
def rank_batch(db: Session = Depends(get_db)):
    jobs = db.scalars(select(Job).where(Job.fit_score.is_(None)).limit(settings.max_jobs_per_batch)).all()
    done = []
    for job in jobs:
        try:
            rank_job(db, job)
            done.append({"id": job.id, "score": job.fit_score})
        except Exception as exc:
            done.append({"id": job.id, "error": str(exc)})
    return done


@app.post("/api/jobs/{job_id}/tailor")
def tailor(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        resume = tailor_job(db, job)
        return {"resume_id": resume.id, "version": resume.version, "reviewer_score": resume.reviewer_score,
                "markdown": resume.markdown, "cover_letter": resume.cover_letter,
                "reviewer_notes": resume.reviewer_notes}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs/{job_id}/resume.pdf")
def resume_pdf(job_id: int, db: Session = Depends(get_db)):
    resume = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id).order_by(desc(ResumeVariant.version)).limit(1))
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    data = markdown_to_simple_pdf(resume.markdown)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="job-{job_id}-resume.pdf"'})


@app.post("/api/jobs/{job_id}/approve")
def approve(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.resumes:
        raise HTTPException(400, "Tailor and review a resume before approval")
    app_obj = approve_job(db, job)
    return {"application_id": app_obj.id, "status": app_obj.status, "apply_url": job.url}


@app.post("/api/jobs/{job_id}/applied")
def applied(job_id: int, payload: RecruiterIn, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        app_obj = mark_applied(db, job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    app_obj.recruiter_name = payload.recruiter_name
    app_obj.recruiter_email = payload.recruiter_email
    app_obj.notes = payload.notes
    db.commit()
    return {"status": app_obj.status, "followup_due_at": app_obj.followup_due_at}


@app.get("/api/profile")
def profile_get(db: Session = Depends(get_db)):
    p = get_profile(db)
    return {c.name: getattr(p, c.name) for c in CandidateProfile.__table__.columns if c.name not in {"updated_at"}}


@app.put("/api/profile")
def profile_put(payload: ProfileIn, db: Session = Depends(get_db)):
    p = get_profile(db)
    for key, value in payload.model_dump().items():
        setattr(p, key, value)
    db.commit()
    return {"ok": True}


@app.get("/api/evidence")
def evidence_get(db: Session = Depends(get_db)):
    items = db.scalars(select(Evidence).order_by(desc(Evidence.id))).all()
    return [{"id": e.id, "company": e.company, "role": e.role, "claim": e.claim,
             "context": e.context, "skills": e.skills, "tags": e.tags, "verified": e.verified} for e in items]


@app.post("/api/evidence")
def evidence_post(payload: EvidenceIn, db: Session = Depends(get_db)):
    e = Evidence(**payload.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id}


def knowledge_dict(question: ATSQuestion, include_history: bool = True) -> dict:
    answers = sorted(question.answers, key=lambda item: item.version, reverse=True)
    occurrences = sorted(question.occurrences, key=lambda item: item.created_at, reverse=True)
    data = {
        "id": question.id, "normalized_text": question.normalized_text,
        "fingerprint": question.fingerprint, "category": question.category,
        "answer_type": question.answer_type, "allowed_choices": question.allowed_choices,
        "sensitivity": question.sensitivity, "archived": question.archived,
        "created_at": question.created_at, "updated_at": question.updated_at,
        "latest_answer": ({"id": answers[0].id, "version": answers[0].version,
                           "answer": answers[0].answer,
                           "verification_state": answers[0].verification_state,
                           "source_application_id": answers[0].source_application_id,
                           "expires_at": answers[0].expires_at} if answers else None),
        "conflict_warning": len({json.dumps(a.answer, sort_keys=True) for a in answers
                                 if a.verification_state == "verified"}) > 1,
    }
    if include_history:
        data["answer_history"] = [
            {"id": a.id, "version": a.version, "answer": a.answer,
             "verification_state": a.verification_state,
             "source_application_id": a.source_application_id, "expires_at": a.expires_at,
             "created_at": a.created_at} for a in answers
        ]
        data["usage_history"] = [
            {"occurrence_id": o.id, "application_id": o.application_id,
             "answer_version_id": o.answer_id, "raw_question_text": o.raw_question_text,
             "match_kind": o.match_kind, "match_confidence": o.match_confidence,
             "resolution_state": o.resolution_state, "provenance": o.provenance,
             "conflict_warning": o.conflict_warning, "created_at": o.created_at}
            for o in occurrences
        ]
    return data


@app.get("/api/knowledge")
def knowledge_search(q: str = "", category: str = "", fingerprint: str = "",
                     include_archived: bool = False, db: Session = Depends(get_db)):
    statement = select(ATSQuestion)
    if fingerprint:
        statement = statement.where(ATSQuestion.fingerprint == fingerprint)
    if category:
        statement = statement.where(ATSQuestion.category == category)
    if not include_archived:
        statement = statement.where(ATSQuestion.archived.is_(False))
    questions = db.scalars(statement.order_by(desc(ATSQuestion.updated_at))).all()
    normalized = normalize_question(q) if q else ""
    results = []
    for question in questions:
        confidence = semantic_confidence(normalized, question.normalized_text) if normalized else 1.0
        if normalized and normalized not in question.normalized_text and confidence < 0.55:
            continue
        item = knowledge_dict(question, include_history=False)
        item["match_confidence"] = confidence
        item["match_kind"] = "exact" if q and question_fingerprint(q) == question.fingerprint else (
            "semantic" if q else "listing")
        item["provenance"] = {"canonical_question_id": question.id,
                              "latest_answer_id": item["latest_answer"]["id"] if item["latest_answer"] else None}
        results.append(item)
    return results


@app.post("/api/knowledge")
def knowledge_create(payload: ATSQuestionIn, db: Session = Depends(get_db)):
    fingerprint = question_fingerprint(payload.text)
    if db.scalar(select(ATSQuestion).where(ATSQuestion.fingerprint == fingerprint)):
        raise HTTPException(409, "A canonical question with this fingerprint already exists")
    question = ATSQuestion(normalized_text=normalize_question(payload.text), fingerprint=fingerprint,
                           category=payload.category, answer_type=payload.answer_type,
                           allowed_choices=payload.allowed_choices, sensitivity=payload.sensitivity)
    db.add(question)
    db.commit()
    db.refresh(question)
    return knowledge_dict(question)


@app.get("/api/knowledge/{question_id}")
def knowledge_get(question_id: int, db: Session = Depends(get_db)):
    question = db.get(ATSQuestion, question_id)
    if not question:
        raise HTTPException(404, "Knowledge question not found")
    return knowledge_dict(question)


@app.patch("/api/knowledge/{question_id}")
def knowledge_update(question_id: int, payload: ATSQuestionUpdate, db: Session = Depends(get_db)):
    question = db.get(ATSQuestion, question_id)
    if not question:
        raise HTTPException(404, "Knowledge question not found")
    changes = payload.model_dump(exclude_unset=True)
    if "text" in changes:
        question.normalized_text = normalize_question(changes.pop("text"))
        question.fingerprint = question_fingerprint(question.normalized_text)
    for key, value in changes.items():
        setattr(question, key, value)
    db.commit()
    return knowledge_dict(question)


@app.delete("/api/knowledge/{question_id}")
def knowledge_archive(question_id: int, db: Session = Depends(get_db)):
    question = db.get(ATSQuestion, question_id)
    if not question:
        raise HTTPException(404, "Knowledge question not found")
    question.archived = True
    db.commit()
    return {"ok": True, "archived": True}


@app.post("/api/knowledge/{question_id}/answers")
def knowledge_answer(question_id: int, payload: ATSAnswerIn, db: Session = Depends(get_db)):
    question = db.get(ATSQuestion, question_id)
    if not question:
        raise HTTPException(404, "Knowledge question not found")
    try:
        answer = create_answer_version(db, question, **payload.model_dump())
        db.commit()
        db.refresh(answer)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": answer.id, "version": answer.version}


@app.post("/api/applications/{application_id}/questions/inspect")
def inspect_questions(application_id: int, payload: ATSInspectionIn, db: Session = Depends(get_db)):
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    occurrences = inspect_application_questions(
        db, application, [item.model_dump() for item in payload.questions], settings.ats_reuse_confidence)
    return [{"id": o.id, "question_id": o.question_id, "answer_version_id": o.answer_id,
             "resolution_state": o.resolution_state, "match_kind": o.match_kind,
             "match_confidence": o.match_confidence, "provenance": o.provenance,
             "conflict_warning": o.conflict_warning} for o in occurrences]


@app.post("/api/applications/{application_id}/questions/{occurrence_id}/resolve")
def resolve_question(application_id: int, occurrence_id: int, payload: ATSResolutionIn,
                     db: Session = Depends(get_db)):
    occurrence = db.get(ATSQuestionOccurrence, occurrence_id)
    if not occurrence or occurrence.application_id != application_id:
        raise HTTPException(404, "Question occurrence not found")
    try:
        answer = resolve_occurrence(db, occurrence, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"answer_version_id": answer.id, "version": answer.version,
            "resolution_state": occurrence.resolution_state,
            "conflict_warning": occurrence.conflict_warning, "provenance": occurrence.provenance}


@app.post("/api/cron/followups")
def cron_followups(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    if settings.cron_secret and x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Invalid cron secret")
    try:
        count = create_due_followups(db)
        return {"created": count}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
