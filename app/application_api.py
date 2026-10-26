"""Applying from the Apply kit (Phase 5): the pre-submit claim and the application execution worker.

    POST /api/jobs/{id}/submission/start     → apply_queue.begin_submission (client apply_kit): a token, or why not
    POST /api/jobs/{id}/applied              → (app.main) records through apply_queue.record_submission
    POST /api/jobs/{id}/submission/release   → nothing was submitted: free the posting
    POST /api/jobs/{id}/submission/resolve   → a person says what happened to a submission whose outcome is unknown
    GET  /api/jobs/{id}/submission           → the posting's open claim and the job's latest execution

    POST /api/jobs/{id}/application-execution            → queue the execution worker (OBSERVE, ASSIST, GUARDED_AUTO)
    POST /api/application-executions/{id}/confirm        → the person confirms sensitive answers for this execution
    POST /api/application-executions/{id}/continue       → the person finished in the kept browser window (takeover)
    POST /api/application-executions/{id}/signup         → a pending login with a generated password, shown once
    POST /api/application-executions/{id}/save-login     → keep the window's login (session) and the password

    GET/PUT    /api/settings/guarded-application          → the account's guarded-submission consent and attestations
    GET/PUT/DELETE /api/settings/ats-credentials[/{domain}] → saved ATS logins (the password is write-only)
    GET/DELETE /api/settings/ats-sessions[/{domain}]       → saved ATS sessions
    GET        /api/application-routes                     → the account's ATS map
    GET        /api/application-backends                   → the backends and the browser-use Web UI's health

Every endpoint acts on the signed-in account only. Nothing here submits an application: guarded submission happens
only inside the execution worker (app.services.submission).
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.models import ApplicationClaim, AtsCredential, Job
from app.services import apply_queue

router = APIRouter()


class ReleaseIn(BaseModel):
    claim_token: str = Field(min_length=8, max_length=64)
    reason: str = Field(default="Not submitted", max_length=500)


class ResolveIn(BaseModel):
    claim_id: int
    submitted: bool
    confirmation_id: str | None = Field(default=None, max_length=200)


class ExecutionIn(BaseModel):
    mode: Literal["OBSERVE", "ASSIST", "GUARDED_AUTO"] | None = None
    backend: Literal["browser_use_webui"] | None = None  # "Run with browser-use": a person's choice of backend


class ConsentIn(BaseModel):
    enabled: bool
    attestations: list[str] = Field(default_factory=list, max_length=50)


class CredentialIn(BaseModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=500)


class SaveLoginIn(BaseModel):
    username: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=500)


class ConfirmIn(BaseModel):
    question_ids: list[int] = Field(min_length=1, max_length=50)


def _job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def claim_view(claim: ApplicationClaim | None, *, with_token: bool = False,
               job_id: int | None = None) -> dict[str, Any] | None:
    """A claim for the screens. The token only for the Apply kit's own open claim on the job being viewed (it reports
    with it): another saved posting of the same job shows the claim as someone else's submission in progress, since
    its token is bound to that other job and would only be refused here."""
    if claim is None:
        return None
    out = {"id": claim.id, "job_id": claim.job_id, "status": claim.status, "client": claim.client,
           "claimed_at": claim.claimed_at.isoformat() if claim.claimed_at else None,
           "lease_expires_at": claim.lease_expires_at.isoformat() if claim.lease_expires_at else None,
           "started_by": apply_queue.SUBMITTED_BY.get(claim.client, claim.client)}
    if with_token and claim.client == "apply_kit" and claim.status == apply_queue.CLAIMED and claim.job_id == job_id:
        out["claim_token"] = claim.token
    return out


@router.get("/api/jobs/{job_id}/submission")
def submission(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    from app.services import application_execution

    job = _job(db, job_id)
    apply_queue.expire_claims(db, user_id=job.user_id)
    task = application_execution.latest(db, job.id)
    from app.services import submission

    intent = submission.intents(db, task.id)[-1] if task is not None and submission.intents(db, task.id) else None
    return {"job_id": job.id, "claim": claim_view(apply_queue.open_claim(db, job), with_token=True, job_id=job.id),
            "not_ready_reason": apply_queue.not_ready_reason(db, job),
            "execution": application_execution.view(task) if task is not None else None,
            "intent": submission.view(intent),
            "takeover": _takeover_view(task),
            "execution_confirmations": application_execution.confirmations(db, task) if task is not None else [],
            "execution_mode": application_execution.available_mode(db),
            "warning": "Applications you submit entirely outside Job Hunt OS cannot be locked here: start the "
                       "submission from the Apply kit (or Claude Desktop) so no one else submits it at the same time."}


def _takeover_view(task: Any) -> dict[str, Any] | None:
    waiting = (task.interrupt or {}) if task is not None else {}
    if task is None or task.status != "PENDING" or task.stage != "NEEDS_HUMAN" or not waiting.get("takeover"):
        return None
    return {"reason": waiting.get("reason"), "expires_at": waiting.get("expires_at"),
            "login_domain": waiting.get("login_domain"), "continued": bool(waiting.get("continued")),
            "fields": [{"id": f.get("id"), "label": f.get("label")} for f in waiting.get("fields") or []]}


@router.post("/api/jobs/{job_id}/submission/start")
def start_submission(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    job = _job(db, job_id)
    result = apply_queue.begin_submission(db, job, client="apply_kit")
    if not result.ok:
        raise HTTPException(409, {"message": result.reason, "status": result.status,
                                  "conflict_job_id": result.conflict_job_id,
                                  "claim": claim_view(result.claim)})
    return {"status": "CLAIMED", "claim": claim_view(result.claim, with_token=True, job_id=job.id)}


@router.post("/api/jobs/{job_id}/submission/release")
def release_submission(job_id: int, payload: ReleaseIn, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    job = _job(db, job_id)
    try:
        claim = apply_queue.release_claim(db, job, payload.claim_token, payload.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": claim.status, "claim": claim_view(claim)}


@router.post("/api/jobs/{job_id}/submission/resolve")
def resolve_submission(job_id: int, payload: ResolveIn, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    job = _job(db, job_id)
    try:
        result = apply_queue.resolve_uncertain(db, job, payload.claim_id, submitted=payload.submitted,
                                               confirmation_id=payload.confirmation_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if isinstance(result, ApplicationClaim):
        return {"status": result.status, "claim": claim_view(result)}
    if not result.ok:
        raise HTTPException(409, {"message": result.reason, "status": result.status,
                                  "duplicate_of_job_id": result.duplicate_job_id})
    return {"status": "applied", "applied_at": result.application.applied_at.isoformat()}


@router.post("/api/jobs/{job_id}/application-execution")
def queue_execution(job_id: int, payload: ExecutionIn, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    from app.services import application_execution

    job = _job(db, job_id)
    try:
        task = application_execution.enqueue(db, job, execution_mode=payload.mode, backend=payload.backend,
                                             reason="browser_use_chosen" if payload.backend else "user_request")
    except application_execution.ExecutionRefused as exc:
        raise HTTPException(409, {"message": str(exc), "code": exc.code}) from exc
    db.commit()
    return {"execution": application_execution.view(task)}


@router.post("/api/application-executions/{task_id}/confirm")
def confirm_answers(task_id: int, payload: ConfirmIn, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    from app.services import application_execution

    try:
        task = application_execution.confirm_answers(db, task_id, payload.question_ids)
    except application_execution.ExecutionRefused as exc:
        raise HTTPException(409, {"message": str(exc), "code": exc.code}) from exc
    return {"execution": application_execution.view(task)}


# ───────────── takeover, signup and saved logins (Phase 6) ─────────────

def _task(db: Session, task_id: int) -> Any:
    from app.models import ApplicationExecutionTask

    task = db.get(ApplicationExecutionTask, task_id)
    if task is None or task.user_id != db.info.get("user_id"):  # tenancy filters it too; never trust one check
        raise HTTPException(404, "Execution not found")
    return task


def _login_domain(db: Session, task: Any) -> str:
    from app.services import ats_map

    domain = (task.interrupt or {}).get("login_domain")
    if not domain:
        match = ats_map.for_job(_job(db, task.job_id))
        domain = match.login_domain or match.apply_domain if match else None
    if not domain:
        raise HTTPException(409, "This application has no login domain")
    return domain


@router.post("/api/application-executions/{task_id}/continue")
def continue_execution(task_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    from app.services import application_execution

    try:
        task = application_execution.continue_after_takeover(db, task_id)
    except application_execution.ExecutionRefused as exc:
        raise HTTPException(409, {"message": str(exc), "code": exc.code}) from exc
    return {"execution": application_execution.view(task)}


@router.post("/api/application-executions/{task_id}/signup")
def begin_signup(task_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """The run stopped at a signup: a pending login for the site with the profile's email and a generated password,
    returned this once so the person types exactly these into the site's signup form."""
    from app.services import ats_auth
    from app.services.workflow import get_profile

    task = _task(db, task_id)
    domain = _login_domain(db, task)
    try:
        row, password = ats_auth.begin_signup(db, task.user_id, domain, get_profile(db).email or "")
    except ats_auth.AuthRefused as exc:
        raise HTTPException(409, {"message": str(exc), "code": exc.code}) from exc
    db.commit()
    return {"domain": row.domain, "username": row.username, "password": password, "status": row.status,
            "note": "Use exactly these on the site's signup form, then click Save this login. The password is not "
                    "shown again."}


@router.post("/api/application-executions/{task_id}/save-login")
def save_login(task_id: int, payload: SaveLoginIn, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Keep the login the person just made in the kept window: the password (typed here, or the pending one from a
    signup) in the vault, and the window's session for that domain only."""
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
    from app.services import ats_auth

    from app.services.workflow import get_profile

    task = _task(db, task_id)
    domain = _login_domain(db, task)
    pending = db.scalar(select(AtsCredential).where(AtsCredential.user_id == task.user_id,
                                                    AtsCredential.domain == domain))
    username = payload.username or (pending.username if pending else None) or get_profile(db).email
    try:
        credential = ats_auth.save_login(db, task.user_id, domain, username=username, password=payload.password)
    except ats_auth.AuthRefused as exc:
        raise HTTPException(409, {"message": str(exc), "code": exc.code}) from exc
    saved_session = False
    manager = get_sidecars()
    session = manager.get(task.user_id, task.id)  # only this account's own execution's window
    if session is not None:
        try:
            state = manager.run_threadsafe(session.call("storage_state"), timeout=30).get("state") or {}
            saved_session = ats_auth.save_session(db, task.user_id, domain, state, ats=task.route) is not None
        except Exception:  # noqa: BLE001  the password is still saved; the session is optional
            saved_session = False
    db.commit()
    return {"credential": ats_auth.credential_view(credential), "session_saved": saved_session}


@router.get("/api/settings/guarded-application")
def guarded_consent(db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import application_execution, submission
    from app.settings import get_settings

    return {**submission.consent(db, user.id), "server_mode": application_execution.available_mode(db),
            "allowlisted_routes": sorted(get_settings().guarded_routes)}


@router.put("/api/settings/guarded-application")
def set_guarded_consent(payload: ConsentIn, db: Session = Depends(get_user_db),
                        user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import submission

    result = submission.set_consent(db, user.id, enabled=payload.enabled, attestations=payload.attestations)
    db.commit()
    return result


@router.get("/api/settings/ats-credentials")
def ats_credentials(db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import ats_auth

    return {"credentials": ats_auth.credentials(db, user.id), "sessions": ats_auth.sessions(db, user.id)}


@router.put("/api/settings/ats-credentials/{domain}")
def put_ats_credential(domain: str, payload: CredentialIn, db: Session = Depends(get_user_db),
                       user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import ats_auth

    try:
        row = ats_auth.save_credential(db, user.id, domain, payload.username, payload.password)
    except ats_auth.AuthRefused as exc:
        raise HTTPException(422, {"message": str(exc), "code": exc.code}) from exc
    db.commit()
    return {"credential": ats_auth.credential_view(row)}


@router.delete("/api/settings/ats-credentials/{domain}")
def delete_ats_credential(domain: str, db: Session = Depends(get_user_db),
                          user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import ats_auth

    try:
        deleted = ats_auth.delete_credential(db, user.id, domain)
    except ats_auth.AuthRefused as exc:
        raise HTTPException(422, {"message": str(exc), "code": exc.code}) from exc
    db.commit()
    return {"deleted": deleted}


@router.delete("/api/settings/ats-sessions/{domain}")
def revoke_ats_session(domain: str, db: Session = Depends(get_user_db),
                       user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import ats_auth

    try:
        revoked = ats_auth.revoke_session(db, user.id, domain)
    except ats_auth.AuthRefused as exc:
        raise HTTPException(422, {"message": str(exc), "code": exc.code}) from exc
    db.commit()
    return {"revoked": revoked}


@router.get("/api/application-routes")
def application_routes(db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import AtsRoute
    from app.services import ats_map

    ats_map.refresh(db, user.id)
    db.commit()
    rows = db.scalars(select(AtsRoute).where(AtsRoute.user_id == user.id).order_by(AtsRoute.ats, AtsRoute.tenant)
                      .limit(500)).all()
    return {"routes": [ats_map.view(db, r) for r in rows]}


@router.get("/api/application-backends")
def application_backends_status(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    from app.services import application_backends, browser_use_webui

    out: dict[str, Any] = {"backends": application_backends.status()}
    if user.is_admin:  # the Web UI probe starts a process and calls a local service: an operator's check
        out["browser_use_webui"] = browser_use_webui.probe()
    return out


@router.post("/api/application-executions/{task_id}/save-window-answers")
def save_window_answers(task_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Keep what the person typed in the open browser window for fields nothing verified could fill: each one becomes
    their verified answer to that question in the Knowledge bank, reused on the next application. Read straight from
    the run's own window into the database; never returned, logged or stored on the run."""
    from app.infrastructure.application_browser.browser_use_sidecar import get_sidecars
    from app.models import KnowledgeAnswer
    from app.services.ats_forms import ApplicationForm, generic
    from app.services.knowledge import question_key

    task = _task(db, task_id)
    entries = (task.plan or {}).get("fields") or {}
    person = [fid for fid, e in entries.items() if e.get("status") == "PERSON"]
    if not person or not task.form:
        raise HTTPException(409, "Nothing you typed in the window is waiting to be saved")
    manager = get_sidecars()
    session = manager.get(task.user_id, task.id)
    if session is None:
        raise HTTPException(409, "The browser window is closed; answer these questions in the Knowledge bank")
    form = ApplicationForm.model_validate(task.form)
    manager.run_threadsafe(session.call("observe"), timeout=60)
    saved = 0
    for field in form.fields:
        key = generic.key_of(field.locator)
        if field.id not in person or key is None or field.kind == "file":
            continue
        try:
            read = manager.run_threadsafe(session.call("read", key=key), timeout=30)
        except Exception:  # noqa: BLE001  a field that left the page is skipped
            continue
        value = ("Yes" if read.get("checked") else "No") if field.kind == "checkbox" else \
            str(read.get("value") or "").strip()
        if not value:
            continue
        question_id = entries[field.id].get("question_id")
        row = db.get(KnowledgeAnswer, question_id) if question_id else db.scalar(
            select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == question_key(field.label)))
        if row is None:
            row = KnowledgeAnswer(user_id=task.user_id, question=field.label, question_key=question_key(field.label),
                                  category="general", answer_type="choice" if field.options else "text",
                                  source_job_id=task.job_id)
            db.add(row)
        row.answer, row.verified = value, True
        saved += 1
    db.commit()
    return {"saved": saved}
