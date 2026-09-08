from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db
from app.models import Application, CandidateProfile, Evidence, FollowUp, Job, ResumeDocument, ResumeVariant, SearchRun
from app.schemas import ApifySearchIn, EvidenceIn, ManualJobIn, ProfileIn, RecruiterIn
from app.services.exports import markdown_to_simple_pdf
from app.services.documents import DocumentError, LocalDocumentStorage, extract_document, validate_document
from app.services.sources import import_greenhouse, import_lever, refresh_apify_run, start_apify_search, upsert_job
from app.services.workflow import approve_job, create_due_followups, get_profile, mark_applied, rank_job, tailor_job
from app.settings import get_settings

Base.metadata.create_all(bind=engine)
settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.3.0")
document_storage = LocalDocumentStorage(settings.resume_storage_path)


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
    stats = {
        "jobs": db.scalar(select(func.count(Job.id))) or 0,
        "shortlisted": db.scalar(select(func.count(Job.id)).where(Job.status == "shortlisted")) or 0,
        "tailored": db.scalar(select(func.count(Job.id)).where(Job.status == "tailored")) or 0,
        "applied": db.scalar(select(func.count(Job.id)).where(Job.status == "applied")) or 0,
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context={ "jobs": jobs, "profile": profile, "evidence": evidence,
        "applications": applications, "followups": followups, "search_runs": search_runs, "stats": stats,
        "resume_document": profile.resume_document,
        "llm_provider": settings.llm_provider, "apify_actor_id": settings.apify_actor_id,
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
@app.patch("/api/profile")
def profile_put(payload: ProfileIn, db: Session = Depends(get_db)):
    p = get_profile(db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, key, value)
    db.commit()
    return {"ok": True}


def document_dict(document: ResumeDocument, include_text: bool = False) -> dict:
    result = {
        "id": document.id,
        "original_filename": document.original_filename,
        "media_type": document.media_type,
        "checksum": document.checksum,
        "size": document.size,
        "uploaded_at": document.uploaded_at.isoformat(),
        "extraction_status": document.extraction_status,
        "download_url": "/api/profile/resume/download",
    }
    if include_text:
        result["extracted_text"] = document.extracted_text
    return result


async def save_resume_upload(upload: UploadFile, db: Session) -> dict:
    data = bytearray()
    while chunk := await upload.read(64 * 1024):
        data.extend(chunk)
        if len(data) > settings.resume_max_upload_bytes:
            raise HTTPException(413, f"Resume exceeds {settings.resume_max_upload_bytes} byte limit")
    filename = upload.filename or ""
    try:
        extension, media_type = validate_document(filename, upload.content_type, bytes(data))
        extracted_text = extract_document(bytes(data), extension)
    except DocumentError as exc:
        raise HTTPException(422, str(exc)) from exc

    profile = get_profile(db)
    old_document = profile.resume_document
    old_key = old_document.storage_key if old_document else None
    storage_key = f"{profile.id}/{uuid.uuid4().hex}{extension}"
    try:
        document_storage.put(storage_key, bytes(data))
        if old_document:
            old_document.original_filename = filename
            old_document.media_type = media_type
            old_document.storage_key = storage_key
            old_document.checksum = hashlib.sha256(data).hexdigest()
            old_document.size = len(data)
            old_document.uploaded_at = datetime.now(UTC).replace(tzinfo=None)
            old_document.extraction_status = "complete"
            old_document.extracted_text = extracted_text
            document = old_document
        else:
            document = ResumeDocument(
                profile_id=profile.id, original_filename=filename, media_type=media_type,
                storage_key=storage_key, checksum=hashlib.sha256(data).hexdigest(), size=len(data),
                extraction_status="complete", extracted_text=extracted_text,
            )
            db.add(document)
        # This is the canonical text workflow.py already consumes.
        profile.master_resume = extracted_text
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        document_storage.delete(storage_key)
        raise
    if old_key and old_key != storage_key:
        document_storage.delete(old_key)
    return document_dict(document, include_text=True)


@app.get("/api/profile/resume")
def resume_metadata(include_text: bool = False, db: Session = Depends(get_db)):
    document = get_profile(db).resume_document
    if not document:
        raise HTTPException(404, "No resume document uploaded")
    return document_dict(document, include_text)


@app.post("/api/profile/resume", status_code=201)
async def resume_upload(file: UploadFile = File(...), db: Session = Depends(get_db)):
    return await save_resume_upload(file, db)


@app.put("/api/profile/resume")
async def resume_replace(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not get_profile(db).resume_document:
        raise HTTPException(404, "No resume document to replace")
    return await save_resume_upload(file, db)


@app.get("/api/profile/resume/download")
def resume_download(db: Session = Depends(get_db)):
    document = get_profile(db).resume_document
    if not document:
        raise HTTPException(404, "No resume document uploaded")
    path = document_storage.path(document.storage_key)
    if not path.is_file():
        raise HTTPException(404, "Stored resume file is missing")
    return FileResponse(path, media_type=document.media_type, filename=document.original_filename)


@app.delete("/api/profile/resume", status_code=204)
def resume_delete(db: Session = Depends(get_db)):
    profile = get_profile(db)
    document = profile.resume_document
    if not document:
        raise HTTPException(404, "No resume document uploaded")
    storage_key = document.storage_key
    db.delete(document)
    profile.master_resume = ""
    db.commit()
    document_storage.delete(storage_key)


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


@app.post("/api/cron/followups")
def cron_followups(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    if settings.cron_secret and x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Invalid cron secret")
    try:
        count = create_due_followups(db)
        return {"created": count}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
