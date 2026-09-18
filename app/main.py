from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app import auth
from app.auth import PASSWORD_MIN_LENGTH, CurrentUser, current_user, get_user_db
from app.auth import router as auth_router
from app.db import engine
from app.db_migrations import run_migrations
from app.hunter_api import router as hunter_router
from app.monitoring_api import router as monitoring_router
from app.job_description_api import router as job_description_router
from app.resume_api import router as resume_router
from app.models import (
    Application,
    CandidateProfile,
    Evidence,
    Job,
    KnowledgeAnswer,
    ResumeDocument,
    ResumeVariant,
    SchedulerJob,
    SearchRun,
)
from app.scheduler import build_scheduler
from app.scheduler_hooks import run_soon
from app.schemas import (
    McpTokenIn,
    ApifySearchIn,
    ApiKeysIn,
    ApplicationUpdateIn,
    BulkTailorIn,
    EvidenceIn,
    KnowledgeIn,
    ManualJobIn,
    ProfileIn,
    ProfilePatch,
    RecruiterIn,
    SettingsIn,
    SettingsPatch,
    TailorTasksCancelIn,
    TailorTasksIn,
)
from app.security import SecretsUnavailable
from app.services import mcp_tokens, ranker, resume_verification, tailor_tasks
from app.services.ai_config import PROVIDERS as AI_PROVIDERS
from app.services.ai_config import STAGES as AI_STAGES
from app.services.ai_config import env_defaults as ai_env_defaults
from app.services.ai_config import resolve as resolve_ai_config
from app.services.contacts import recruiters_of, set_primary_recruiter
from app.services.exports import markdown_to_simple_pdf
from app.services.structured_resume import clean_raw_text
from app.services.sources import (
    import_greenhouse,
    import_lever,
    refresh_apify_run,
    start_apify_search,
    upsert_job,
)
from app.services.user_settings import (
    EXECUTION_MODES,
    execution_mode_for,
    key_status,
    lock_settings_row,
    set_user_keys,
    settings_for,
)
from app.services.workflow import (
    SKIP_TAILORING_STATUSES,
    approve_job,
    get_profile,
    mark_applied,
    skip_tailoring,
    tailor_job,
)
from app.settings import get_settings
from app.tenancy import require_user_id
from app.workspace import router as workspace_router

run_migrations(engine)
settings = get_settings()
# One scheduler per process. Manual triggers use it even when the background loop is disabled.
scheduler = build_scheduler()
tailor_worker = tailor_tasks.build_worker()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if scheduler.enabled:
        await scheduler.start()
    if tailor_worker.enabled:
        await tailor_worker.start()
    try:
        yield
    finally:
        await tailor_worker.stop()
        await scheduler.stop()


app = FastAPI(title=settings.app_name, version="0.5.0", lifespan=lifespan)
app.state.scheduler = scheduler


@app.exception_handler(RequestValidationError)
async def safe_password_validation(request: Request, exc: RequestValidationError):
    if request.url.path == "/api/auth/password":
        errors = [{k: error[k] for k in ("loc", "msg", "type")} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})
    return await request_validation_exception_handler(request, exc)


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(auth_router)
app.include_router(workspace_router)
app.include_router(hunter_router)
app.include_router(monitoring_router)
app.include_router(resume_router)
app.include_router(job_description_router)

# Reachable without signing in. Cron and admin routes are protected by their own secrets.
PUBLIC_PATHS = {"/health", "/login", "/signup", "/api/auth/login", "/api/auth/signup", "/api/auth/logout"}
TOKEN_PREFIXES = ("/api/cron/", "/api/admin/")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _cross_site(request: Request) -> bool:
    origin = request.headers.get("origin")
    return bool(origin) and urlparse(origin).netloc != request.headers.get("host", "")


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """Every page and API call needs a signed-in user, except the sign-in flow, static files and health."""
    path = request.url.path
    if request.method not in SAFE_METHODS and _cross_site(request):
        return JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)
    request.state.user = None
    if not (path == "/health" or path.startswith("/static/") or path.startswith(TOKEN_PREFIXES)):
        request.state.user = await run_in_threadpool(auth.authenticate, request)
    if request.state.user is None and not (path in PUBLIC_PATHS or path.startswith("/static/")
                                           or path.startswith(TOKEN_PREFIXES)):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


templates = Jinja2Templates(directory="app/templates")


def question_key(question: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def job_dict(j: Job) -> dict:
    return {
        "id": j.id, "title": j.title, "company": j.company, "location": j.location,
        "url": j.url, "source": j.source, "fit_score": j.fit_score,
        "fit_analysis": j.fit_analysis, "status": j.status, "discovered_at": j.discovered_at.isoformat(),
    }


def _asset_version() -> str:
    # Cache-bust static assets whenever they change, not only on version bumps.
    static = Path(__file__).parent / "static"
    return f"{app.version}-{max(int((static / name).stat().st_mtime) for name in ('app.js', 'app.css', 'settings.js', 'knowledge.js'))}"


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(UTC).isoformat()}


# ───────────── sign-in pages ─────────────

def _auth_page(request: Request, mode: str):
    if request.state.user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="auth.html", context={
        "mode": mode, "endpoint": f"/api/auth/{mode}", "allow_signup": settings.allow_signup,
        "password_min": PASSWORD_MIN_LENGTH, "version": _asset_version()})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _auth_page(request, "login")


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return _auth_page(request, "signup")


# ───────────── scheduler ─────────────

@app.get("/api/system/scheduler")
def scheduler_status(user: CurrentUser = Depends(current_user)):
    """Scheduler state on this instance plus each job's lease and last run, shared by all instances.

    Job results and errors span every account, so only the admin sees them.
    """
    status = scheduler.status()
    if not user.is_admin:
        for job in status["jobs"]:
            job.update(last_result=None, last_error=None, locked_by=None)
    return status


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = settings.admin_token
    if not expected:
        raise HTTPException(403, "Manual scheduler triggers are disabled until ADMIN_TOKEN is set")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(401, "Invalid admin token")


@app.post("/api/admin/scheduler/{job_name}/run", dependencies=[Depends(require_admin)])
async def scheduler_run(job_name: str):
    """Run one scheduled job now, with the same code and database lease as the scheduler."""
    name = job_name.replace("-", "_")
    if name not in scheduler.specs:
        raise HTTPException(404, f"Unknown scheduled job '{job_name}'. Jobs: {', '.join(scheduler.specs)}")
    outcome = await scheduler.run_job(name, trigger="manual")
    if outcome.status == "busy":
        raise HTTPException(409, outcome.error)
    return outcome.public()


# ───────────── workspace shell and settings ─────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_user_db)):
    # The workspace is a client-rendered app; the shell only carries boot data.
    app_setting = settings_for(db)
    profile = get_profile(db)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "app_setting": app_setting, "profile": profile, "max_upload_mb": settings.max_resume_upload_mb,
        "version": _asset_version(), "user": request.state.user.public(),
    })


@app.get("/api/settings")
def settings_get(db: Session = Depends(get_user_db)):
    value = settings_for(db)
    keys = key_status(db)
    configured = {name: keys[name]["configured"] for name in ("openrouter", "openai", "anthropic", "gemini", "apify")}
    overrides = (value.preferences or {}).get("ai") or {}
    effective = resolve_ai_config(settings, overrides)
    return {"llm_provider": effective["llm_provider"], "model": effective["model"],
            "application_mode": value.application_mode, "min_review_score": value.min_review_score,
            "execution_mode": execution_mode_for(db),
            "configured": {**configured, "mock": True}, "keys": keys,
            "secret_storage": bool(settings.secret_key),
            # Raw saved values (blank = inherit) plus the server defaults, for the editor.
            "ai": {"saved": {"llm_provider": overrides.get("llm_provider") or "", "model": overrides.get("model") or "",
                             "stage_models": overrides.get("stage_models") or {},
                             "require_free": overrides.get("require_free")},
                   "env": ai_env_defaults(settings), "effective": effective}}


@app.put("/api/settings")
def settings_put(payload: SettingsIn, db: Session = Depends(get_user_db)):
    provider = payload.llm_provider.strip().lower()
    if provider and provider not in AI_PROVIDERS:
        raise HTTPException(422, "Unsupported LLM provider")
    if payload.application_mode not in {"manual_approval", "automatic"}:
        raise HTTPException(422, "Unsupported application mode")
    if payload.execution_mode is not None and payload.execution_mode not in EXECUTION_MODES:
        raise HTTPException(422, "Unsupported execution mode")
    unknown = set(payload.stage_models) - {stage for stage, _ in AI_STAGES}
    if unknown:
        raise HTTPException(422, f"Unknown stage(s): {', '.join(sorted(unknown))}")
    value = settings_for(db)
    execution_mode = payload.execution_mode or execution_mode_for(db)
    ai = {"llm_provider": provider, "model": payload.model.strip(),
          "stage_models": {k: v.strip()[:300] for k, v in payload.stage_models.items() if v.strip()},
          "require_free": payload.require_free}
    value.preferences = {**(value.preferences or {}), "ai": ai, "execution_mode": execution_mode}
    effective = resolve_ai_config(settings, ai)
    value.llm_provider, value.model = effective["llm_provider"], effective["model"][:300]
    value.application_mode, value.min_review_score = payload.application_mode, payload.min_review_score
    db.commit()
    return settings_get(db)


@app.put("/api/settings/keys")
def settings_keys_put(payload: ApiKeysIn, db: Session = Depends(get_user_db)):
    """Save or remove the signed-in user's own API keys. Omitted keys are unchanged; "" removes one."""
    try:
        set_user_keys(db, payload.model_dump())
    except SecretsUnavailable as exc:
        raise HTTPException(400, f"{exc}. Ask the server admin to set SECRET_KEY.") from exc
    return settings_get(db)


@app.patch("/api/settings")
def settings_patch(payload: SettingsPatch, db: Session = Depends(get_user_db)):
    """Merge independent settings editors without resetting other overrides."""
    row = settings_for(db)
    row = lock_settings_row(db, row)
    changes = payload.model_dump(exclude_unset=True)
    ai = dict((row.preferences or {}).get("ai") or {})
    stages = {**ai.get("stage_models", {}), **changes.pop("stage_models", {})}
    current = {**ai, "stage_models": stages, "application_mode": row.application_mode,
               "min_review_score": row.min_review_score, "execution_mode": execution_mode_for(db), **changes}
    return settings_put(SettingsIn(**current), db)


# ───────────── Claude Desktop MCP tokens ─────────────

@app.get("/api/settings/mcp-tokens")
def mcp_tokens_list(db: Session = Depends(get_user_db)):
    return {"tokens": mcp_tokens.list_tokens(db)}


@app.post("/api/settings/mcp-tokens")
def mcp_tokens_create(payload: McpTokenIn, db: Session = Depends(get_user_db)):
    """Create a token for the local MCP server. The plaintext is returned only in this response."""
    row, token = mcp_tokens.create_token(db, payload.name)
    return {**mcp_tokens.token_dict(row), "token": token}


@app.delete("/api/settings/mcp-tokens/{token_id}")
def mcp_tokens_revoke(token_id: int, db: Session = Depends(get_user_db)):
    if not mcp_tokens.revoke_token(db, token_id):
        raise HTTPException(404, "Token not found")
    return {"revoked": True}


# ───────────── jobs ─────────────

@app.get("/api/jobs")
def list_jobs(db: Session = Depends(get_user_db)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(desc(Job.discovered_at))).all()]


@app.post("/api/jobs/manual")
def add_job(payload: ManualJobIn, db: Session = Depends(get_user_db)):
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
def lever(site: str, db: Session = Depends(get_user_db)):
    return _board_import(import_lever, db, site, "Lever")


@app.post("/api/import/greenhouse/{board_token}")
def greenhouse(board_token: str, db: Session = Depends(get_user_db)):
    return _board_import(import_greenhouse, db, board_token, "Greenhouse")


@app.post("/api/search/apify")
def apify_search(payload: ApifySearchIn, db: Session = Depends(get_user_db)):
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
def apify_run_status(run_id: int, reimport: bool = False, db: Session = Depends(get_user_db)):
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    try:
        return refresh_apify_run(db, run, reimport=reimport)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/search/apify/runs")
def apify_runs(db: Session = Depends(get_user_db)):
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


@app.post("/api/jobs/{job_id}/rank", status_code=202)
def rank(job_id: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue a (re-)ranking of one job. The ranker runs asynchronously; poll /api/ranking/tasks for the result."""
    if not db.get(Job, job_id):
        raise HTTPException(404, "Job not found")
    out = ranker.request_ranking(db, [job_id], force=True, actor=f"user:{require_user_id(db)}")
    if out["skipped"]:
        raise HTTPException(409, out["skipped"][0]["reason"])
    run_soon(background, request.app, "ranking")
    return out


@app.post("/api/jobs/rank-batch", status_code=202)
def rank_batch(request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue every job that has no ranking yet."""
    ids = db.scalars(select(Job.id).where(Job.fit_score.is_(None), Job.state.in_(ranker.RANK_WAITING_STATES))
                     .order_by(Job.id).limit(5000)).all()
    out = ranker.request_ranking(db, ids, force=False, only_unranked=True, actor=f"user:{require_user_id(db)}")
    run_soon(background, request.app, "ranking")
    return out


@app.get("/api/ranking/tasks")
def ranking_tasks(job_ids: str = "", db: Session = Depends(get_user_db)):
    """The latest ranking task of each listed job (comma-separated ids)."""
    try:
        ids = [int(part) for part in job_ids.split(",") if part.strip()][:500]
    except ValueError as exc:
        raise HTTPException(422, "job_ids must be comma-separated integers") from exc
    tasks = ranker.task_summaries(db, ids)
    return {"tasks": tasks, "active": sum(t["status"] in ("PENDING", "PROCESSING") for t in tasks)}


@app.get("/api/ranking/tracker")
def ranking_tracker(request: Request, db: Session = Depends(get_user_db)):
    """The ranking tracker: this account's waiting, running, retrying and recently finished ranking work."""
    out = ranker.tracker(db, require_user_id(db))
    worker = getattr(request.app.state, "scheduler", None)
    row = db.get(SchedulerJob, "ranking")
    live = bool(row and row.lock_expires_at and row.lock_expires_at > datetime.now(UTC).replace(tzinfo=None))
    # Only schedule facts: the job's results and errors span every account, so they stay admin-only.
    out["worker"] = {"enabled": bool(worker and worker.enabled and worker.interval("ranking") > 0),
                     "interval_minutes": worker.interval("ranking") if worker else 0, "running": live,
                     "next_run_at": row.next_run_at.isoformat() if row and row.next_run_at else None,
                     "last_finished_at": row.last_finished_at.isoformat() if row and row.last_finished_at else None}
    return out


@app.get("/api/jobs/{job_id}/rankings")
def job_rankings(job_id: int, db: Session = Depends(get_user_db)):
    """Every ranking version of a job, newest first. Earlier versions are never overwritten."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job.id, "state": job.state.value,
            "rankings": [ranker.ranking_dict(r) for r in sorted(job.rankings, key=lambda r: -r.version)],
            "tasks": [ranker.task_dict(t) for t in sorted(job.ranking_tasks, key=lambda t: -t.id)]}


@app.post("/api/jobs/{job_id}/tailor")
def tailor(job_id: int, db: Session = Depends(get_user_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        return _packet_response(db, job, tailor_job(db, job))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/{job_id}/skip-tailoring")
def skip_tailor(job_id: int, db: Session = Depends(get_user_db)):
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
def bulk_tailor(payload: BulkTailorIn, db: Session = Depends(get_user_db)):
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
    return tailor_tasks.packet_outcome(db, job, resume)


@app.post("/api/tailor-tasks")
def queue_tailor_tasks(payload: TailorTasksIn, db: Session = Depends(get_user_db)):
    """Queue tailoring (or skip tailoring) for jobs; a background worker runs them, so the page can be left."""
    return tailor_tasks.enqueue(db, payload.ids, payload.mode, payload.regenerate)


@app.get("/api/tailor-tasks")
def list_tailor_tasks(db: Session = Depends(get_user_db)):
    return tailor_tasks.list_tasks(db)


@app.post("/api/tailor-tasks/cancel")
def cancel_tailor_tasks(payload: TailorTasksCancelIn, db: Session = Depends(get_user_db)):
    return {"cancelled": tailor_tasks.cancel_queued(db, payload.ids)}


@app.post("/api/tailor-tasks/clear")
def clear_tailor_tasks(db: Session = Depends(get_user_db)):
    return {"cleared": tailor_tasks.clear_finished(db)}


@app.get("/api/jobs/{job_id}/resume.pdf")
def resume_pdf(job_id: int, db: Session = Depends(get_user_db)):
    resume = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id).order_by(desc(ResumeVariant.version)).limit(1))
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    data = markdown_to_simple_pdf(resume.markdown)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="job-{job_id}-resume.pdf"'})


@app.post("/api/jobs/{job_id}/approve")
def approve(job_id: int, db: Session = Depends(get_user_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.resumes:
        raise HTTPException(400, "Tailor and review a resume before approval")
    latest = max(job.resumes, key=lambda item: item.version)
    min_score = settings_for(db).min_review_score
    if latest.reviewer_score is None or latest.reviewer_score < min_score:
        raise HTTPException(400, f"Resume review score must be at least {min_score}")
    try:
        app_obj = approve_job(db, job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"application_id": app_obj.id, "status": app_obj.status, "apply_url": job.url}


@app.post("/api/jobs/{job_id}/applied")
def applied(job_id: int, payload: RecruiterIn, db: Session = Depends(get_user_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        app_obj = mark_applied(db, job)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Only overwrite what the confirmation step actually supplied.
    if payload.recruiter_name or payload.recruiter_email:
        set_primary_recruiter(app_obj, payload.recruiter_name or app_obj.recruiter_name,
                              payload.recruiter_email or app_obj.recruiter_email)
    app_obj.notes = payload.notes or app_obj.notes
    db.commit()
    return {"status": app_obj.status, "followup_due_at": app_obj.followup_due_at}


@app.get("/api/applications")
def application_list(db: Session = Depends(get_user_db)):
    rows = db.scalars(select(Application).order_by(desc(Application.id))).all()
    return [{"id": item.id, "job_id": item.job_id, "status": item.status,
             "notes": item.notes, "recruiter_name": item.recruiter_name,
             "recruiter_email": item.recruiter_email, "recruiters": recruiters_of(item),
             "approved_resume_id": item.approved_resume_id} for item in rows]


@app.get("/api/jobs/{job_id}/application-detail")
def application_detail(job_id: int, db: Session = Depends(get_user_db)):
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
                "recruiter_email": app_obj.recruiter_email, "recruiters": recruiters_of(app_obj)},
            "questions": [{"id": q.id, "question": q.question, "answer": q.answer,
                            "verified": q.verified, "sensitive": q.sensitive} for q in questions]}


@app.patch("/api/applications/{application_id}")
def application_update(application_id: int, payload: ApplicationUpdateIn, db: Session = Depends(get_user_db)):
    item = db.get(Application, application_id)
    if not item:
        raise HTTPException(404, "Application not found")
    item.notes = payload.notes
    set_primary_recruiter(item, payload.recruiter_name, payload.recruiter_email)
    if payload.status:
        if payload.status not in {"pending_approval", "rejected"}:
            raise HTTPException(422, "Only pending approval or rejected may be set here")
        item.status = payload.status
        item.job.status = payload.status
    db.commit()
    return {"ok": True, "status": item.status}


# ───────────── profile, resumes, evidence, knowledge ─────────────

@app.get("/api/profile")
def profile_get(db: Session = Depends(get_user_db)):
    p = get_profile(db)
    return {c.name: getattr(p, c.name) for c in CandidateProfile.__table__.columns if c.name not in {"updated_at", "user_id"}}


@app.put("/api/profile")
def profile_put(payload: ProfileIn, db: Session = Depends(get_user_db)):
    p = get_profile(db)
    values = payload.model_dump(exclude_none=True)
    for key, value in values.items():
        setattr(p, key, value)
    if "master_resume" in values:
        db.flush()
        resume_verification.active_resume(db, p)  # edited master text becomes its own resume version
    db.commit()
    return {"ok": True}


@app.patch("/api/profile")
def profile_patch(payload: ProfilePatch, db: Session = Depends(get_user_db)):
    """Save supplied fields only, preserving newer resume text and other editors."""
    row = get_profile(db)
    row = lock_settings_row(db, row)
    changes = payload.model_dump(exclude_unset=True)
    if "search_preferences" in changes:
        changes["search_preferences"] = {**(row.search_preferences or {}), **changes["search_preferences"]}
    for key, value in changes.items():
        setattr(row, key, value)
    if "master_resume" in changes:
        db.flush()
        resume_verification.active_resume(db, row)  # edited master text becomes its own resume version
    db.commit()
    return profile_get(db)


@app.post("/api/profile/resume")
async def resume_upload(file: UploadFile = File(...), db: Session = Depends(get_user_db)):
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
    extracted = clean_raw_text(extracted)
    if not extracted.strip():
        raise HTTPException(422, "No readable text was found in the resume")
    digest = hashlib.sha256(data).hexdigest()
    root = Path(settings.upload_dir) / str(require_user_id(db))  # each account's files in its own folder
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
        existing.active, existing.extracted_text, existing.storage_path = True, extracted, str(path)
        document = existing
    else:
        db.add(document)
    profile = get_profile(db)
    profile.master_resume = extracted
    db.flush()
    # A new resume version (or the existing one for this exact file), parsed and unverified until reviewed.
    version = resume_verification.record_version(db, profile, extracted, document=document, source="upload")
    db.commit()
    db.refresh(document)
    return {"id": document.id, "filename": document.original_filename, "size": document.size,
            "resume_id": version.id, "resume_version": version.version}


@app.get("/api/profile/resume")
def resume_download(db: Session = Depends(get_user_db)):
    document = db.scalar(select(ResumeDocument).where(ResumeDocument.active.is_(True)).order_by(desc(ResumeDocument.id)))
    if not document or not Path(document.storage_path).is_file():
        raise HTTPException(404, "No uploaded resume")
    return FileResponse(document.storage_path, media_type=document.media_type,
                        filename=document.original_filename)


@app.delete("/api/profile/resume")
def resume_delete(db: Session = Depends(get_user_db)):
    document = db.scalar(select(ResumeDocument).where(ResumeDocument.active.is_(True)).order_by(desc(ResumeDocument.id)))
    if not document:
        raise HTTPException(404, "No uploaded resume")
    document.active = False
    db.commit()
    return {"ok": True}


@app.get("/api/evidence")
def evidence_get(db: Session = Depends(get_user_db)):
    items = db.scalars(select(Evidence).order_by(desc(Evidence.id))).all()
    return [{"id": e.id, "company": e.company, "role": e.role, "claim": e.claim,
             "context": e.context, "skills": e.skills, "tags": e.tags, "verified": e.verified} for e in items]


@app.post("/api/evidence")
def evidence_post(payload: EvidenceIn, db: Session = Depends(get_user_db)):
    e = Evidence(**payload.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id}


@app.get("/api/knowledge")
def knowledge_get(db: Session = Depends(get_user_db)):
    rows = db.scalars(select(KnowledgeAnswer).order_by(desc(KnowledgeAnswer.updated_at))).all()
    return [{"id": q.id, "question": q.question, "answer": q.answer, "category": q.category,
             "answer_type": q.answer_type, "verified": q.verified, "sensitive": q.sensitive,
             "source_job_id": q.source_job_id, "use_count": q.use_count} for q in rows]


@app.post("/api/knowledge")
def knowledge_post(payload: KnowledgeIn, db: Session = Depends(get_user_db)):
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
def knowledge_delete(item_id: int, db: Session = Depends(get_user_db)):
    item = db.get(KnowledgeAnswer, item_id)
    if not item:
        raise HTTPException(404, "Knowledge answer not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


# ───────────── cron (external schedulers) ─────────────

def _check_cron_secret(x_cron_secret: str | None) -> None:
    if settings.cron_secret and not secrets.compare_digest(x_cron_secret or "", settings.cron_secret):
        raise HTTPException(401, "Invalid cron secret")


async def _cron_run(name: str) -> dict:
    outcome = await scheduler.run_job(name, trigger="cron")
    if outcome.status == "busy":
        raise HTTPException(409, outcome.error)
    return outcome.public()


@app.post("/api/cron/followups")
async def cron_followups(x_cron_secret: str | None = Header(default=None)):
    """Draft due follow-ups for every account, with each account's own AI key."""
    _check_cron_secret(x_cron_secret)
    return await _cron_run("followups")


@app.post("/api/cron/searches")
async def cron_searches(x_cron_secret: str | None = Header(default=None)):
    """Start the saved searches of every account whose schedule is not manual."""
    _check_cron_secret(x_cron_secret)
    return await _cron_run("hunter")
