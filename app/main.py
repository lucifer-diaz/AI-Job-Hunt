from __future__ import annotations

import base64
import hashlib
import secrets
import re
from pathlib import Path
from datetime import UTC, datetime
from io import BytesIO

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db import engine, get_db
from app.db_migrations import run_migrations
from app.models import (AppSetting, Application, CandidateProfile, Evidence, Job,
                        KnowledgeAnswer, ResumeDocument, ResumeVariant, SearchRun)
from app.schemas import (ApplicationUpdateIn, ApifySearchIn, BulkTailorIn, EvidenceIn, KnowledgeIn,
                         ManualJobIn, ProfileIn, RecruiterIn, SettingsIn)
from app.services.exports import markdown_to_simple_pdf
from app.services.sources import import_greenhouse, import_lever, refresh_apify_run, start_apify_search, upsert_job
from app.services.workflow import (SKIP_TAILORING_STATUSES, approve_job, create_due_followups, get_profile,
                                   mark_applied, rank_job, skip_tailoring, tailor_job)
from app.services.ai_config import PROVIDERS as AI_PROVIDERS, STAGES as AI_STAGES, env_defaults as ai_env_defaults
from app.services.ai_config import resolve as resolve_ai_config
from app.settings import get_settings
from app.workspace import router as workspace_router, run_search_profile, get_search_profile

run_migrations(engine)
settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.5.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(workspace_router)


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


def get_app_setting(db: Session) -> AppSetting:
    value = db.get(AppSetting, 1)
    if not value:
        value = AppSetting(id=1, llm_provider=settings.llm_provider,
                           model=getattr(settings, f"{settings.llm_provider}_model", ""))
        db.add(value)
        db.commit()
        db.refresh(value)
    return value


def question_key(question: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


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
    # The workspace is a client-rendered app; the shell only carries boot data.
    app_setting = get_app_setting(db)
    profile = get_profile(db)
    # Cache-bust static assets whenever they change, not only on version bumps.
    static = Path(__file__).parent / "static"
    stamp = max(int((static / name).stat().st_mtime) for name in ("app.js", "app.css"))
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "app_setting": app_setting, "profile": profile, "max_upload_mb": settings.max_resume_upload_mb,
        "version": f"{app.version}-{stamp}",
    })


@app.get("/api/settings")
def settings_get(db: Session = Depends(get_db)):
    value = get_app_setting(db)
    configured = {
        "openrouter": bool(settings.openrouter_api_key), "openai": bool(settings.openai_api_key),
        "anthropic": bool(settings.anthropic_api_key), "mock": True,
    }
    overrides = (value.preferences or {}).get("ai") or {}
    effective = resolve_ai_config(settings, overrides)
    return {"llm_provider": effective["llm_provider"], "model": effective["model"],
            "application_mode": value.application_mode, "min_review_score": value.min_review_score,
            "configured": configured,
            # Raw saved values (blank = inherit) plus what .env would supply, for the editor.
            "ai": {"saved": {"llm_provider": overrides.get("llm_provider") or "", "model": overrides.get("model") or "",
                             "stage_models": overrides.get("stage_models") or {},
                             "require_free": overrides.get("require_free")},
                   "env": ai_env_defaults(settings), "effective": effective}}


@app.put("/api/settings")
def settings_put(payload: SettingsIn, db: Session = Depends(get_db)):
    provider = payload.llm_provider.strip().lower()
    if provider and provider not in AI_PROVIDERS:
        raise HTTPException(422, "Unsupported LLM provider")
    if payload.application_mode not in {"manual_approval", "automatic"}:
        raise HTTPException(422, "Unsupported application mode")
    unknown = set(payload.stage_models) - {stage for stage, _ in AI_STAGES}
    if unknown:
        raise HTTPException(422, f"Unknown stage(s): {', '.join(sorted(unknown))}")
    value = get_app_setting(db)
    ai = {"llm_provider": provider, "model": payload.model.strip(),
          "stage_models": {k: v.strip()[:300] for k, v in payload.stage_models.items() if v.strip()},
          "require_free": payload.require_free}
    value.preferences = {**(value.preferences or {}), "ai": ai}
    effective = resolve_ai_config(settings, ai)
    value.llm_provider, value.model = effective["llm_provider"], effective["model"][:300]
    value.application_mode, value.min_review_score = payload.application_mode, payload.min_review_score
    db.commit()
    return settings_get(db)


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


def _board_import(importer, db: Session, token: str, label: str):
    try:
        return importer(db, token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, f"No public {label} board named '{token}'") from exc
        raise HTTPException(502, f"{label} returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach {label}: {exc.__class__.__name__}") from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/import/lever/{site}")
def lever(site: str, db: Session = Depends(get_db)):
    return _board_import(import_lever, db, site, "Lever")


@app.post("/api/import/greenhouse/{board_token}")
def greenhouse(board_token: str, db: Session = Depends(get_db)):
    return _board_import(import_greenhouse, db, board_token, "Greenhouse")


@app.post("/api/search/apify")
def apify_search(payload: ApifySearchIn, db: Session = Depends(get_db)):
    values = payload.model_dump()
    locations = list(dict.fromkeys(location.strip() for location in payload.locations if location.strip()))
    if not locations:
        locations = [payload.location.strip()]
    runs = []
    try:
        for location in locations:
            run = start_apify_search(db, {**values, "location": location})
            runs.append({"id": run.id, "external_run_id": run.external_run_id,
                         "actor_id": run.actor_id, "status": run.status,
                         "location": location})
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**runs[0], "runs": runs}


@app.get("/api/search/apify/runs/{run_id}")
def apify_run_status(run_id: int, reimport: bool = False, db: Session = Depends(get_db)):
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    try:
        return refresh_apify_run(db, run, reimport=reimport)
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
        return _packet_response(db, job, tailor_job(db, job))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/{job_id}/skip-tailoring")
def skip_tailor(job_id: int, db: Session = Depends(get_db)):
    """Send the master resume unchanged for a job that needs no tailoring. It is still reviewed before approval."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in SKIP_TAILORING_STATUSES:
        raise HTTPException(409, "Only shortlisted or tailored jobs that are not yet approved can skip tailoring")
    try:
        return _packet_response(db, job, skip_tailoring(db, job))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/bulk-tailor")
def bulk_tailor(payload: BulkTailorIn, db: Session = Depends(get_db)):
    """Tailor (or skip tailoring for) selected Tailor-queue jobs a batch at a time; call again with `remaining`.

    Only shortlisted, tailored or sent-back jobs are processed, and jobs that already have a packet are
    skipped unless `regenerate` is set. One job failing does not stop the batch.
    """
    wanted = list(dict.fromkeys(payload.ids))
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_(wanted))).all()}
    has_packet = set(db.scalars(select(ResumeVariant.job_id).where(ResumeVariant.job_id.in_(wanted))).all())
    queue = [i for i in wanted if i in jobs and jobs[i].status in SKIP_TAILORING_STATUSES
             and (payload.regenerate or i not in has_packet)]
    batch, remaining = queue[:payload.batch_size], queue[payload.batch_size:]
    run = skip_tailoring if payload.mode == "skip" else tailor_job
    done, errors = [], []
    for job_id in batch:
        try:
            out = _packet_response(db, jobs[job_id], run(db, jobs[job_id]))
            done.append({"id": job_id, "reviewer_score": out["reviewer_score"], "auto_approved": out["auto_approved"],
                         "untailored": out["untailored"]})
        except Exception as exc:
            db.rollback()
            errors.append({"id": job_id, "error": str(exc)[:300]})
    found = [i for i in wanted if i in jobs]
    return {"done": done, "errors": errors, "remaining": remaining,
            "skipped": len(found) - len(queue), "missing": len(wanted) - len(found)}


def _packet_response(db: Session, job: Job, resume: ResumeVariant) -> dict:
    auto_approved = False
    app_setting = get_app_setting(db)
    if app_setting.application_mode == "automatic" and (resume.reviewer_score or 0) >= app_setting.min_review_score:
        approve_job(db, job)
        auto_approved = True
    return {"resume_id": resume.id, "version": resume.version, "reviewer_score": resume.reviewer_score,
            "markdown": resume.markdown, "cover_letter": resume.cover_letter,
            "reviewer_notes": resume.reviewer_notes, "auto_approved": auto_approved,
            "untailored": bool((resume.content or {}).get("untailored"))}


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
    latest = max(job.resumes, key=lambda item: item.version)
    if latest.reviewer_score is None or latest.reviewer_score < get_app_setting(db).min_review_score:
        raise HTTPException(400, f"Resume review score must be at least {get_app_setting(db).min_review_score}")
    try:
        app_obj = approve_job(db, job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
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
    # Only overwrite what the confirmation step actually supplied.
    app_obj.recruiter_name = payload.recruiter_name or app_obj.recruiter_name
    app_obj.recruiter_email = payload.recruiter_email or app_obj.recruiter_email
    app_obj.notes = payload.notes or app_obj.notes
    db.commit()
    return {"status": app_obj.status, "followup_due_at": app_obj.followup_due_at}


@app.get("/api/applications")
def application_list(db: Session = Depends(get_db)):
    rows = db.scalars(select(Application).order_by(desc(Application.id))).all()
    return [{"id": item.id, "job_id": item.job_id, "status": item.status,
             "notes": item.notes, "recruiter_name": item.recruiter_name,
             "recruiter_email": item.recruiter_email,
             "approved_resume_id": item.approved_resume_id} for item in rows]


@app.get("/api/jobs/{job_id}/application-detail")
def application_detail(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    resume = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job.id)
                       .order_by(desc(ResumeVariant.version)).limit(1))
    app_obj = job.application
    questions = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.source_job_id == job.id)).all()
    return {"job": {**job_dict(job), "description": job.description},
            "resume": None if not resume else {"id": resume.id, "version": resume.version,
                "markdown": resume.markdown, "cover_letter": resume.cover_letter,
                "reviewer_score": resume.reviewer_score, "reviewer_notes": resume.reviewer_notes},
            "application": None if not app_obj else {"id": app_obj.id, "status": app_obj.status,
                "notes": app_obj.notes, "recruiter_name": app_obj.recruiter_name,
                "recruiter_email": app_obj.recruiter_email},
            "questions": [{"id": q.id, "question": q.question, "answer": q.answer,
                            "verified": q.verified, "sensitive": q.sensitive} for q in questions]}


@app.patch("/api/applications/{application_id}")
def application_update(application_id: int, payload: ApplicationUpdateIn, db: Session = Depends(get_db)):
    item = db.get(Application, application_id)
    if not item:
        raise HTTPException(404, "Application not found")
    item.notes, item.recruiter_name, item.recruiter_email = payload.notes, payload.recruiter_name, payload.recruiter_email
    if payload.status:
        if payload.status not in {"pending_approval", "rejected"}:
            raise HTTPException(422, "Only pending approval or rejected may be set here")
        item.status = payload.status
        item.job.status = payload.status
    db.commit()
    return {"ok": True, "status": item.status}


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


@app.post("/api/profile/resume")
async def resume_upload(file: UploadFile = File(...), db: Session = Depends(get_db)):
    allowed = {"text/plain", "application/pdf",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    if file.content_type not in allowed:
        raise HTTPException(415, "Upload a TXT, PDF, or DOCX resume")
    data = await file.read(settings.max_resume_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_resume_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Resume exceeds {settings.max_resume_upload_mb} MB")
    if not data:
        raise HTTPException(400, "Resume is empty")
    try:
        if file.content_type == "text/plain":
            extracted = data.decode("utf-8")
        elif file.content_type == "application/pdf":
            from pypdf import PdfReader
            extracted = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
        else:
            from docx import Document
            extracted = "\n".join(p.text for p in Document(BytesIO(data)).paragraphs)
    except Exception as exc:
        raise HTTPException(422, f"Could not read resume: {exc}") from exc
    if not extracted.strip():
        raise HTTPException(422, "No readable text was found in the resume")
    digest = hashlib.sha256(data).hexdigest()
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / digest
    path.write_bytes(data)
    for old in db.scalars(select(ResumeDocument).where(ResumeDocument.active.is_(True))).all():
        old.active = False
    document = ResumeDocument(original_filename=Path(file.filename or "resume").name,
                              media_type=file.content_type, storage_path=str(path), checksum=digest,
                              size=len(data), extracted_text=extracted, active=True)
    existing = db.scalar(select(ResumeDocument).where(ResumeDocument.checksum == digest))
    if existing:
        existing.active, existing.extracted_text = True, extracted
        document = existing
    else:
        db.add(document)
    profile = get_profile(db)
    profile.master_resume = extracted
    db.commit()
    db.refresh(document)
    return {"id": document.id, "filename": document.original_filename, "size": document.size}


@app.get("/api/profile/resume")
def resume_download(db: Session = Depends(get_db)):
    document = db.scalar(select(ResumeDocument).where(ResumeDocument.active.is_(True)).order_by(desc(ResumeDocument.id)))
    if not document or not Path(document.storage_path).is_file():
        raise HTTPException(404, "No uploaded resume")
    return FileResponse(document.storage_path, media_type=document.media_type,
                        filename=document.original_filename)


@app.delete("/api/profile/resume")
def resume_delete(db: Session = Depends(get_db)):
    document = db.scalar(select(ResumeDocument).where(ResumeDocument.active.is_(True)).order_by(desc(ResumeDocument.id)))
    if not document:
        raise HTTPException(404, "No uploaded resume")
    document.active = False
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


@app.get("/api/knowledge")
def knowledge_get(db: Session = Depends(get_db)):
    rows = db.scalars(select(KnowledgeAnswer).order_by(desc(KnowledgeAnswer.updated_at))).all()
    return [{"id": q.id, "question": q.question, "answer": q.answer, "category": q.category,
             "answer_type": q.answer_type, "verified": q.verified, "sensitive": q.sensitive,
             "source_job_id": q.source_job_id, "use_count": q.use_count} for q in rows]


@app.post("/api/knowledge")
def knowledge_post(payload: KnowledgeIn, db: Session = Depends(get_db)):
    key = question_key(payload.question)
    item = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == key))
    if item:
        if item.verified and item.answer != payload.answer and not payload.verified:
            raise HTTPException(409, "A verified answer already exists; verify the replacement explicitly")
        for field, value in payload.model_dump().items():
            setattr(item, field, value)
    else:
        item = KnowledgeAnswer(question_key=key, **payload.model_dump())
        db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "verified": item.verified}


@app.delete("/api/knowledge/{item_id}")
def knowledge_delete(item_id: int, db: Session = Depends(get_db)):
    item = db.get(KnowledgeAnswer, item_id)
    if not item:
        raise HTTPException(404, "Knowledge answer not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@app.post("/api/cron/followups")
def cron_followups(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    if settings.cron_secret and x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Invalid cron secret")
    try:
        count = create_due_followups(db)
        return {"created": count}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cron/searches")
def cron_searches(x_cron_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    """Run the saved search profile when its schedule is daily. Call once a day from a scheduler."""
    if settings.cron_secret and x_cron_secret != settings.cron_secret:
        raise HTTPException(401, "Invalid cron secret")
    profile = get_search_profile(db)
    if profile.get("schedule") != "daily":
        return {"started": 0, "reason": "schedule is manual"}
    try:
        runs = run_search_profile(db, profile)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"started": len(runs), "run_ids": [r.id for r in runs]}
