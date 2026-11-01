"""Resume Data API: structured resume review, editing and verification (Settings → Resumes).

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
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.auth import get_user_db
from app.models import Resume, ResumeDocument, SavedSearch
from app.services import resume_matching, resume_parsing
from app.services import resume_verification as rv
from app.services.resume_extraction import store as extraction_store
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
            "is_active": resume.is_active,
            "verification_status": resume.verification_status, "verified_at": _iso(resume.verified_at),
            "parser": resume.parser, "parser_version": resume.parser_version, "revision": resume.revision,
            "created_at": _iso(resume.created_at), "updated_at": _iso(resume.updated_at),
            "has_pending_reparse": bool(resume.pending_reparse),
            # What it is for (app.services.resume_matching): new jobs are matched to the best-tagged resume.
            "domain": resume.domain, "roles": resume.roles or [], "domains": resume_matching.groups(resume)}


def detail(resume: Resume) -> dict:
    db = object_session(resume)
    return {**summary(resume), "structured": resume.structured_data, "progress": rv.progress(resume.structured_data),
            "pending_reparse": resume.pending_reparse, "sections": SECTION_LABELS,
            # The latest AI structuring of this version (app.services.resume_parsing): status and counts only.
            "ai_parse": resume_parsing.status_of(db, resume.id) if db is not None else None,
            # How its file was read (resume extraction V2): quality route and the sections found; None = unknown.
            "extraction": extraction_store.summary(db, resume.id) if db is not None else None}


def _commit(db: Session, resume: Resume) -> dict:
    db.commit()
    db.refresh(resume)
    return detail(resume)


def _setups_by_resume(db: Session) -> dict[int | None, list[dict]]:
    """Search setups by the resume they pin (None: setups that always use the active resume)."""
    out: dict[int | None, list[dict]] = {}
    for setup in db.scalars(select(SavedSearch).order_by(SavedSearch.id)).all():
        settings = setup.settings or {}
        chosen = settings.get("resume_id")
        mode = settings.get("resume_mode") or ("fixed" if isinstance(chosen, int) else "auto")
        if mode == "fixed" and isinstance(chosen, int):
            out.setdefault(chosen, []).append({"id": setup.id, "name": setup.name})
        elif mode == "active":
            out.setdefault(None, []).append({"id": setup.id, "name": setup.name})
    return out


def _card(version: Resume, files: dict[int, ResumeDocument], setups: list[dict]) -> dict:
    """One resume as Settings → Resumes lists it: the version, its uploaded file, how much of its data is reviewed,
    the roles it targets (its tags) and the setups that use it."""
    document = files.get(version.resume_document_id) if version.resume_document_id else None
    return {**summary(version), "setups": setups, "reviewed_percent": rv.progress(version.structured_data)["percent"],
            "file": {"id": document.id, "filename": document.original_filename, "size": document.size,
                     "media_type": document.media_type} if document is not None else None}


@router.get("/api/settings/resume")
def settings_resume(resume_id: int | None = Query(None), db: Session = Depends(get_user_db)):
    """Every resume (one per version, each with its own file, structured data and target roles), the active one,
    the one being viewed (`current`: `resume_id`, else the active one) with its verification progress, and the
    candidate policy.

    Viewing or editing a resume never makes it active: only POST /api/resumes/{id}/activate does."""
    profile = profile_for(db)
    active = rv.active_resume(db, profile)
    files = {d.id: d for d in db.scalars(select(ResumeDocument)).all()}
    linked = set(db.scalars(select(Resume.resume_document_id).where(Resume.resume_document_id.is_not(None))).all())
    for document in files.values():  # a file uploaded before versions existed gets its resume (inactive) now
        if document.id not in linked:
            rv.version_of_document(db, profile, document)
    all_versions = rv.versions(db, profile)
    for version in all_versions:
        rv.ensure_structured(db, version)
    db.commit()
    current = next((v for v in all_versions if v.id == resume_id), None) if resume_id else None
    current = current or active
    setups = _setups_by_resume(db)
    return {"active": detail(active) if active else None,
            "current": detail(current) if current else None,
            "versions": [_card(v, files, setups.get(v.id, []) + (setups.get(None, []) if v.is_active else []))
                         for v in all_versions],
            "policy": candidate_policy(db), "ai_only": resume_parsing.ai_only(db, profile.user_id),
            "extraction_v2": resume_parsing.extraction_v2(db, profile.user_id)}


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


class SectionAssignIn(BaseModel):
    line: int = Field(ge=0)
    kind: str = Field(max_length=40)


@router.post("/api/resumes/{resume_id}/sections")
def assign_section(resume_id: int, payload: SectionAssignIn, db: Session = Depends(get_user_db)):
    """Put a heading the reader found in a section (or say it is none): the data is re-read with it."""
    resume = _resume(db, resume_id)
    try:
        out = extraction_store.assign(db, resume, payload.line, payload.kind)
    except extraction_store.UnknownHeading as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {**_commit(db, resume), "section_result": out}


@router.post("/api/resumes/{resume_id}/reparse")
def reparse(resume_id: int, payload: ReparseIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    try:
        rv.reparse(db, resume, use_ai=payload.use_ai)  # use_ai only queues the AI parse: no model in the request
    except Exception as exc:  # e.g. AI structuring is off
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
    """Delete a resume (not the active one): its structured data, history and uploaded file. Jobs and setups that
    used it go back to matching by tags, and evidence only it verified is un-verified. A file another resume still
    uses is kept."""
    resume = _resume(db, resume_id)
    document = db.get(ResumeDocument, resume.resume_document_id) if resume.resume_document_id else None
    try:
        out = rv.delete_version(db, resume)
    except rv.ActiveResumeError as exc:
        raise HTTPException(409, str(exc)) from exc
    stored = None
    if document is not None and not db.scalar(select(Resume.id).where(Resume.resume_document_id == document.id)):
        stored = Path(document.storage_path)
        db.delete(document)
    db.commit()
    if (stored is not None and stored.is_file()
            and not db.scalar(select(ResumeDocument.id).where(ResumeDocument.storage_path == str(stored)))
            and not rv.storage_in_use(db, str(stored))):
        stored.unlink(missing_ok=True)
    return {"deleted": resume_id, "file_deleted": stored is not None, **out}

