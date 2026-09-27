"""Resume Data API: structured resume review, editing and verification (Settings → Resume Data).

`{resume_id}` here is a resume version (app.models.Resume). The older `/api/resumes/{id}/edits` and
`/cover-letter` routes in app.workspace address tailored resume variants and are unrelated.
Clients never write raw JSON or verification state: edits go through typed input (StructuredResumeIn) and
verification only through the verify endpoints.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import Resume
from app.services import resume_verification as rv
from app.services.structured_resume import SECTION_LABELS, StructuredResumeIn
from app.services.user_settings import (
    CANDIDATE_POLICY_DEFAULTS,
    candidate_policy,
    lock_settings_row,
    profile_for,
    settings_for,
)
from app.tenancy import require_user_id

router = APIRouter()


class StructuredPutIn(BaseModel):
    revision: int = Field(ge=0)
    data: StructuredResumeIn


class ItemIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=120)
    status: Literal["VERIFIED", "REJECTED"] = "VERIFIED"


class UnverifyIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=120)


class SectionIn(BaseModel):
    section: str = Field(min_length=1, max_length=40)


class ReparseIn(BaseModel):
    use_ai: bool = False


class ResolveIn(BaseModel):
    decisions: dict[str, Literal["keep", "use_new"]] = Field(default_factory=dict)


class PolicyIn(BaseModel):
    resume_source: Literal["structured", "master_text"] | None = None
    allow_unverified_resume_facts: bool | None = None
    ranking_uses_unverified: bool | None = None


def _resume(db: Session, resume_id: int) -> Resume:
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "Resume not found")
    return resume


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def summary(resume: Resume) -> dict:
    return {"id": resume.id, "name": resume.name, "version": resume.version, "source": resume.source,
            "source_filename": resume.source_filename, "source_file_hash": resume.source_file_hash,
            "has_original": bool(resume.source_storage_path and Path(resume.source_storage_path).is_file()),
            "is_active": resume.is_active, "is_default": resume.is_default,
            "verification_status": resume.verification_status, "verified_at": _iso(resume.verified_at),
            "parser": resume.parser, "parser_version": resume.parser_version, "revision": resume.revision,
            "created_at": _iso(resume.created_at), "updated_at": _iso(resume.updated_at),
            "has_pending_reparse": bool(resume.pending_reparse),
            # What it is for (app.services.resume_matching): new jobs are matched to the best-tagged resume.
            "domain": resume.domain, "roles": resume.roles or []}


def detail(resume: Resume) -> dict:
    return {**summary(resume), "structured": resume.structured_data, "progress": rv.progress(resume.structured_data),
            "pending_reparse": resume.pending_reparse, "sections": SECTION_LABELS}


def _commit(db: Session, resume: Resume) -> dict:
    db.commit()
    db.refresh(resume)
    return detail(resume)


@router.get("/api/settings/resume")
def settings_resume(db: Session = Depends(get_user_db)):
    """The active resume with its verification progress, every version, and the candidate policy."""
    profile = profile_for(db)
    active = rv.active_resume(db, profile)
    all_versions = rv.versions(db, profile)
    for version in all_versions:
        rv.ensure_structured(db, version)
    db.commit()
    return {"active": detail(active) if active else None, "versions": [summary(v) for v in all_versions],
            "policy": candidate_policy(db)}


@router.put("/api/settings/resume/policy")
def settings_resume_policy(payload: PolicyIn, db: Session = Depends(get_user_db)):
    row = lock_settings_row(db, settings_for(db))
    prefs = dict(row.preferences or {})
    policy = {**CANDIDATE_POLICY_DEFAULTS, **(prefs.get("candidate_policy") or {}),
              **payload.model_dump(exclude_none=True)}
    row.preferences = {**prefs, "candidate_policy": policy}
    db.commit()
    return candidate_policy(db)


@router.get("/api/resumes/{resume_id}/structured")
def structured_get(resume_id: int, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    rv.ensure_structured(db, resume)
    return _commit(db, resume)


@router.put("/api/resumes/{resume_id}/structured")
def structured_put(resume_id: int, payload: StructuredPutIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.apply_edit(db, resume, payload.data, payload.revision)
    except rv.ResumeConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return _commit(db, resume)


@router.post("/api/resumes/{resume_id}/verify")
def verify(resume_id: int, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.verify_resume(db, resume, require_user_id(db))
    except rv.VerificationBlocked as exc:
        db.rollback()
        raise HTTPException(422, {"message": str(exc), "blocking": exc.blocking}) from exc
    return _commit(db, resume)


@router.post("/api/resumes/{resume_id}/verify-item")
def verify_item(resume_id: int, payload: ItemIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.set_item_status(db, resume, payload.item_id, payload.status)
    except rv.UnknownItem as exc:
        raise HTTPException(404, "Item not found on this resume") from exc
    return _commit(db, resume)


@router.post("/api/resumes/{resume_id}/unverify-item")
def unverify_item(resume_id: int, payload: UnverifyIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.set_item_status(db, resume, payload.item_id, "UNVERIFIED")
    except rv.UnknownItem as exc:
        raise HTTPException(404, "Item not found on this resume") from exc
    return _commit(db, resume)


@router.post("/api/resumes/{resume_id}/verify-section")
def verify_section(resume_id: int, payload: SectionIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.verify_section(db, resume, payload.section)
    except rv.UnknownItem as exc:
        raise HTTPException(404, "Unknown section") from exc
    return _commit(db, resume)


@router.get("/api/resumes/{resume_id}/changes")
def changes(resume_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
            db: Session = Depends(get_user_db)):
    return rv.changes(db, _resume(db, resume_id), limit=limit, offset=offset)


@router.get("/api/resumes/{resume_id}/raw")
def raw_text(resume_id: int, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    return {"id": resume.id, "raw_text": resume.raw_text, "raw_text_hash": resume.raw_text_hash}


@router.get("/api/resumes/{resume_id}/original")
def original(resume_id: int, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    if not resume.source_storage_path or not Path(resume.source_storage_path).is_file():
        raise HTTPException(404, "The original file is not available for this version")
    return FileResponse(resume.source_storage_path, media_type=resume.source_media_type or "application/octet-stream",
                        filename=resume.source_filename or f"resume-v{resume.version}")


@router.post("/api/resumes/{resume_id}/reparse")
def reparse(resume_id: int, payload: ReparseIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.reparse(db, resume, use_ai=payload.use_ai)
    except Exception as exc:  # e.g. no AI key or the parser answered with invalid data
        db.rollback()
        raise HTTPException(400, f"Could not re-parse the resume: {exc}") from exc
    return _commit(db, resume)


@router.post("/api/resumes/{resume_id}/reparse/resolve")
def reparse_resolve(resume_id: int, payload: ResolveIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.resolve_reparse(db, resume, payload.decisions)
    except rv.UnknownItem as exc:
        raise HTTPException(404, str(exc)) from exc
    except rv.ResumeConflict as exc:
        db.commit()  # keep the discarded comparison cleared
        raise HTTPException(409, str(exc)) from exc
    return _commit(db, resume)


@router.post("/api/resumes/{resume_id}/activate")
def activate(resume_id: int, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    rv.set_active(db, resume)
    return _commit(db, resume)


@router.delete("/api/resumes/{resume_id}")
def delete_version(resume_id: int, db: Session = Depends(get_user_db)):
    """Delete a resume version (not the active one). Its uploaded file stays in the library; jobs that used it go
    back to matching by tags, and evidence only it verified is un-verified."""
    resume = _resume(db, resume_id)
    try:
        out = rv.delete_version(db, resume)
    except rv.ActiveResumeError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return {"deleted": resume_id, **out}


@router.post("/api/resumes/{resume_id}/default")
def make_default(resume_id: int, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    rv.set_default(db, resume)
    return _commit(db, resume)
