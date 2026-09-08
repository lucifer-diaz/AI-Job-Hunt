from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db
from app.models import (
    Application,
    CandidateProfile,
    Evidence,
    FollowUp,
    Job,
    ResumeVariant,
    SearchRun,
)
from app.schemas import (
    ApifySearchIn,
    ApplicationDecisionIn,
    ApplicationUpdateIn,
    EvidenceIn,
    ManualJobIn,
    MarkAppliedIn,
    ProfileIn,
)
from app.services.exports import markdown_to_simple_pdf
from app.services.sources import (
    import_greenhouse,
    import_lever,
    refresh_apify_run,
    start_apify_search,
    upsert_job,
)
from app.services.workflow import (
    approve_job,
    change_application_state,
    create_due_followups,
    get_profile,
    mark_applied,
    rank_job,
    tailor_job,
)
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
            authenticated = secrets.compare_digest(
                username, settings.app_username
            ) and secrets.compare_digest(password, settings.app_password)
        except Exception:
            authenticated = False
    if not authenticated:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Job Hunt OS"'})
    return await call_next(request)


templates = Jinja2Templates(directory="app/templates")


def job_dict(j: Job) -> dict:
    return {
        "id": j.id,
        "title": j.title,
        "company": j.company,
        "location": j.location,
        "url": j.url,
        "source": j.source,
        "fit_score": j.fit_score,
        "fit_analysis": j.fit_analysis,
        "status": j.status,
        "discovered_at": j.discovered_at.isoformat(),
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
    stats = {
        "jobs": db.scalar(select(func.count(Job.id))) or 0,
        "shortlisted": db.scalar(select(func.count(Job.id)).where(Job.status == "shortlisted"))
        or 0,
        "tailored": db.scalar(select(func.count(Job.id)).where(Job.status == "tailored")) or 0,
        "applied": db.scalar(select(func.count(Job.id)).where(Job.status == "applied")) or 0,
    }
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "jobs": jobs,
            "profile": profile,
            "evidence": evidence,
            "applications": applications,
            "followups": followups,
            "search_runs": search_runs,
            "stats": stats,
            "llm_provider": settings.llm_provider,
            "apify_actor_id": settings.apify_actor_id,
        },
    )


def _latest_resume(db: Session, job_id: int) -> ResumeVariant | None:
    return db.scalar(
        select(ResumeVariant)
        .where(ResumeVariant.job_id == job_id)
        .order_by(desc(ResumeVariant.version))
        .limit(1)
    )


def _application_row(db: Session, job: Job) -> dict:
    resume = _latest_resume(db, job.id)
    application = job.application
    return {
        "job_id": job.id,
        "role": job.title,
        "company": job.company,
        "fit_score": job.fit_score,
        "resume_version": resume.version if resume else None,
        "reviewer_score": resume.reviewer_score if resume else None,
        "review_state": "completed" if resume and resume.reviewer_score is not None else "pending",
        "application_status": application.status if application else "pending_approval",
        "notes_summary": (application.notes[:140] if application else ""),
    }


@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, db: Session = Depends(get_db)):
    jobs = db.scalars(select(Job).order_by(desc(Job.discovered_at))).all()
    rows = [_application_row(db, job) for job in jobs if job.resumes or job.application]
    return templates.TemplateResponse(
        request=request, name="approvals.html", context={"applications": rows}
    )


@app.get("/api/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(desc(Job.discovered_at))).all()]


@app.get("/api/applications")
def list_applications(status: str | None = None, db: Session = Depends(get_db)):
    valid = {"pending_approval", "approved", "applied", "rejected", "revision_requested"}
    if status is not None and status not in valid:
        raise HTTPException(422, f"Unknown application status '{status}'")
    jobs = db.scalars(select(Job).order_by(desc(Job.discovered_at))).all()
    rows = [_application_row(db, job) for job in jobs if job.resumes or job.application]
    return [row for row in rows if status is None or row["application_status"] == status]


@app.get("/api/jobs/{job_id}/application-detail")
def application_detail(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    resume = _latest_resume(db, job.id)
    application = job.application
    raw = job.raw or {}
    ats_keys = ("ats_questions", "application_questions", "screening_questions", "questions")
    ats_questions = next((raw[key] for key in ats_keys if key in raw), [])
    return {
        "job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "description": job.description,
            "url": job.url,
        },
        "fit_analysis": job.fit_analysis or {},
        "resume": None
        if not resume
        else {
            "id": resume.id,
            "version": resume.version,
            "markdown": resume.markdown,
            "download_url": f"/api/jobs/{job.id}/resume.pdf",
        },
        "cover_letter": resume.cover_letter if resume else "",
        "reviewer_feedback": None
        if not resume
        else {
            "score": resume.reviewer_score,
            "state": "completed" if resume.reviewer_score is not None else "pending",
            "notes": resume.reviewer_notes or {},
        },
        "application": {
            "status": application.status if application else "pending_approval",
            "recruiter_name": application.recruiter_name if application else "",
            "recruiter_email": application.recruiter_email if application else "",
            "notes": application.notes if application else "",
            "approved_resume_id": application.approved_resume_id if application else None,
            "approved_at": application.approved_at.isoformat()
            if application and application.approved_at
            else None,
            "applied_at": application.applied_at.isoformat()
            if application and application.applied_at
            else None,
        },
        "ats_questions": ats_questions,
    }


@app.post("/api/jobs/manual")
def add_job(payload: ManualJobIn, db: Session = Depends(get_db)):
    ref = hashlib.sha256(f"{payload.company}|{payload.title}|{payload.url}".encode()).hexdigest()[
        :40
    ]
    job, created = upsert_job(
        db,
        source="manual",
        source_ref=ref,
        title=payload.title,
        company=payload.company,
        location=payload.location,
        url=payload.url,
        description=payload.description,
    )
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
            "id": r.id,
            "external_run_id": r.external_run_id,
            "actor_id": r.actor_id,
            "status": r.status,
            "item_count": r.item_count,
            "created_jobs": r.created_jobs,
            "created_at": r.created_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
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
    jobs = db.scalars(
        select(Job).where(Job.fit_score.is_(None)).limit(settings.max_jobs_per_batch)
    ).all()
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
        return {
            "resume_id": resume.id,
            "version": resume.version,
            "reviewer_score": resume.reviewer_score,
            "markdown": resume.markdown,
            "cover_letter": resume.cover_letter,
            "reviewer_notes": resume.reviewer_notes,
        }
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs/{job_id}/resume.pdf")
def resume_pdf(job_id: int, db: Session = Depends(get_db)):
    application = db.scalar(select(Application).where(Application.job_id == job_id))
    resume = (
        db.get(ResumeVariant, application.approved_resume_id)
        if application and application.approved_resume_id
        else _latest_resume(db, job_id)
    )
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    data = markdown_to_simple_pdf(resume.markdown)
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}-resume.pdf"'},
    )


@app.post("/api/jobs/{job_id}/approve")
def approve(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        app_obj = approve_job(db, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"application_id": app_obj.id, "status": app_obj.status, "apply_url": job.url}


@app.post("/api/jobs/{job_id}/applied")
def applied(job_id: int, payload: MarkAppliedIn, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not payload.submitted:
        raise HTTPException(422, "Confirm the external application was submitted")
    try:
        app_obj = mark_applied(db, job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    app_obj.recruiter_name = payload.recruiter_name
    app_obj.recruiter_email = payload.recruiter_email
    app_obj.notes = payload.notes
    db.commit()
    return {"status": app_obj.status, "followup_due_at": app_obj.followup_due_at}


@app.put("/api/jobs/{job_id}/application")
def update_application(job_id: int, payload: ApplicationUpdateIn, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    application = job.application
    if not application:
        application = Application(job_id=job.id)
        db.add(application)
    for key, value in payload.model_dump().items():
        setattr(application, key, value)
    db.commit()
    return {"status": application.status, "saved": True}


def _application_decision(job_id: int, target: str, payload: ApplicationDecisionIn, db: Session):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        application = change_application_state(db, job, target, payload.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": application.status}


@app.post("/api/jobs/{job_id}/reject")
def reject_application(job_id: int, payload: ApplicationDecisionIn, db: Session = Depends(get_db)):
    return _application_decision(job_id, "rejected", payload, db)


@app.post("/api/jobs/{job_id}/revision")
def revise_application(job_id: int, payload: ApplicationDecisionIn, db: Session = Depends(get_db)):
    return _application_decision(job_id, "revision_requested", payload, db)


@app.get("/api/profile")
def profile_get(db: Session = Depends(get_db)):
    p = get_profile(db)
    return {
        c.name: getattr(p, c.name)
        for c in CandidateProfile.__table__.columns
        if c.name not in {"updated_at"}
    }


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
    return [
        {
            "id": e.id,
            "company": e.company,
            "role": e.role,
            "claim": e.claim,
            "context": e.context,
            "skills": e.skills,
            "tags": e.tags,
            "verified": e.verified,
        }
        for e in items
    ]


@app.post("/api/evidence")
def evidence_post(payload: EvidenceIn, db: Session = Depends(get_db)):
    e = Evidence(**payload.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id}


@app.post("/api/cron/followups")
def cron_followups(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    if settings.cron_secret and x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Invalid cron secret")
    try:
        count = create_due_followups(db)
        return {"created": count}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
