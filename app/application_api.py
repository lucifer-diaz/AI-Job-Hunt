"""Applying from the Apply kit (Phase 5): the pre-submit claim and the application execution worker.

    POST /api/jobs/{id}/submission/start     → apply_queue.begin_submission (client apply_kit): a token, or why not
    POST /api/jobs/{id}/applied              → (app.main) records through apply_queue.record_submission
    POST /api/jobs/{id}/submission/release   → nothing was submitted: free the posting
    POST /api/jobs/{id}/submission/resolve   → a person says what happened to a submission whose outcome is unknown
    GET  /api/jobs/{id}/submission           → the posting's open claim and the job's latest execution

    POST /api/jobs/{id}/application-execution            → queue the execution worker (OBSERVE or ASSIST)
    POST /api/application-executions/{id}/confirm        → the person confirms sensitive answers for this execution

Every endpoint acts on the signed-in account only. Nothing here submits an application.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import ApplicationClaim, Job
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
    mode: Literal["OBSERVE", "ASSIST"] | None = None


class ConfirmIn(BaseModel):
    question_ids: list[int] = Field(min_length=1, max_length=50)


def _job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def claim_view(claim: ApplicationClaim | None, *, with_token: bool = False) -> dict[str, Any] | None:
    """A claim for the screens. The token only for the Apply kit's own open claim (it reports with it)."""
    if claim is None:
        return None
    out = {"id": claim.id, "job_id": claim.job_id, "status": claim.status, "client": claim.client,
           "claimed_at": claim.claimed_at.isoformat() if claim.claimed_at else None,
           "lease_expires_at": claim.lease_expires_at.isoformat() if claim.lease_expires_at else None,
           "started_by": apply_queue.SUBMITTED_BY.get(claim.client, claim.client)}
    if with_token and claim.client == "apply_kit" and claim.status == apply_queue.CLAIMED:
        out["claim_token"] = claim.token
    return out


@router.get("/api/jobs/{job_id}/submission")
def submission(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    from app.services import application_execution

    job = _job(db, job_id)
    apply_queue.expire_claims(db, user_id=job.user_id)
    task = application_execution.latest(db, job.id)
    return {"job_id": job.id, "claim": claim_view(apply_queue.open_claim(db, job), with_token=True),
            "not_ready_reason": apply_queue.not_ready_reason(db, job),
            "execution": application_execution.view(task) if task is not None else None,
            "execution_mode": application_execution.mode(),
            "warning": "Applications you submit entirely outside Job Hunt OS cannot be locked here: start the "
                       "submission from the Apply kit (or Claude Desktop) so no one else submits it at the same time."}


@router.post("/api/jobs/{job_id}/submission/start")
def start_submission(job_id: int, db: Session = Depends(get_user_db)) -> dict[str, Any]:
    job = _job(db, job_id)
    result = apply_queue.begin_submission(db, job, client="apply_kit")
    if not result.ok:
        raise HTTPException(409, {"message": result.reason, "status": result.status,
                                  "conflict_job_id": result.conflict_job_id,
                                  "claim": claim_view(result.claim)})
    return {"status": "CLAIMED", "claim": claim_view(result.claim, with_token=True)}


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
        task = application_execution.enqueue(db, job, execution_mode=payload.mode)
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
