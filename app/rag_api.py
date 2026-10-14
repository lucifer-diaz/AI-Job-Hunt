"""Candidate knowledge retrieval API: evidence ↔ resume tags, the index status, and a job's evidence pack preview.

Every endpoint acts on the signed-in account only (app.tenancy plus explicit user filters in the services). The
preview and status endpoints are reads: they never index, embed or change a job.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.models import Job
from app.services import evidence_tags, rag_index, rag_retrieval
from app.services.workflow import get_profile

router = APIRouter()


class ResumeTagsIn(BaseModel):
    resume_ids: list[int] = Field(default_factory=list, max_length=100)


class BulkTagsIn(BaseModel):
    evidence_ids: list[int] = Field(min_length=1, max_length=500)
    add: list[int] = Field(default_factory=list, max_length=100)
    remove: list[int] = Field(default_factory=list, max_length=100)


@router.get("/api/evidence/resumes")
def evidence_resume_choices(user: CurrentUser = Depends(current_user),
                            db: Session = Depends(get_user_db)) -> list[dict[str, Any]]:
    """The account's resumes, for tagging evidence: id, name, version, whether active, and how many verified facts
    a job using it may draw on (its own facts, tagged ones and untagged evidence)."""
    from sqlalchemy import desc, select

    from app.models import Resume

    rows = db.scalars(select(Resume).where(Resume.user_id == user.id).order_by(desc(Resume.is_active),
                                                                               desc(Resume.version))).all()
    return [{"id": r.id, "name": r.name or f"Resume v{r.version}", "version": r.version, "is_active": r.is_active,
             "evidence_in_scope": len(evidence_tags.allowed_evidence_ids(db, user.id, r))} for r in rows]


@router.put("/api/evidence/{evidence_id}/resumes")
def set_evidence_resumes(evidence_id: int, payload: ResumeTagsIn, user: CurrentUser = Depends(current_user),
                         db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Make these resumes exactly the ones this evidence belongs to (empty: manual evidence applies to all)."""
    try:
        out = evidence_tags.set_resumes(db, user.id, evidence_id, payload.resume_ids)
    except evidence_tags.TagError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    return {"id": evidence_id, **out}


@router.post("/api/evidence/bulk-resumes")
def bulk_evidence_resumes(payload: BulkTagsIn, user: CurrentUser = Depends(current_user),
                          db: Session = Depends(get_user_db)) -> dict[str, Any]:
    try:
        out = evidence_tags.bulk(db, user.id, payload.evidence_ids, payload.add, payload.remove)
    except evidence_tags.TagError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    return out


@router.get("/api/rag/status")
def rag_status(user: CurrentUser = Depends(current_user), db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """Retrieval mode, embedding availability and the account's index versions (metadata only)."""
    return rag_index.status(db, user.id)


@router.get("/api/rag/preview")
def rag_preview(job_id: int, user: CurrentUser = Depends(current_user),
                db: Session = Depends(get_user_db)) -> dict[str, Any]:
    """The evidence pack retrieval would give this job: in scope, ranked, cited by evidence id. Keyword search only
    (a preview never calls a model), so it shows the scope and the full-text part of a hybrid result."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    profile = get_profile(db)
    pack = rag_retrieval.retrieve_evidence(db, profile, job, allow_model_call=False)
    return pack.to_dict()
