"""Discovery quality API: the domain filter (lists, learned rules, keeping a hidden job) and the resume per job.

Nothing here calls a model. Hidden jobs are listed by GET /api/jobs/search?domain=not_matching.
"""
from __future__ import annotations

import copy
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import Job, Resume
from app.scheduler_hooks import run_soon
from app.services import domain_match, feedback_learning, ranker, resume_matching
from app.services.user_settings import lock_settings_row, settings_for

router = APIRouter()

Terms = Field(default_factory=list, max_length=200)


class DomainIn(BaseModel):
    enabled: bool = True
    exclude_terms: list[str] = Terms
    skill_excludes: list[str] = Terms
    include_terms: list[str] = Terms
    learn_from_feedback: bool = True


class LearnedIn(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    kind: Literal["title", "company"] = "title"
    action: Literal["disable", "enable", "delete", "promote"]


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)


def _user(db: Session) -> int:
    return db.info["user_id"]


def _clean(terms: list[str]) -> list[str]:
    return list(dict.fromkeys(t.strip().lower() for t in terms if t and t.strip()))[:200]


def _save_domain(db: Session, **changes) -> None:
    row = lock_settings_row(db, settings_for(db))
    preferences = copy.deepcopy(row.preferences or {})  # a new object: JSON columns save by identity
    preferences.setdefault("domain", {}).update(changes)
    row.preferences = preferences


def _domain_view(db: Session) -> dict:
    user_id = _user(db)
    prefs, own, rules = domain_match.preferences(db, user_id), domain_match.derived(db, user_id), \
        domain_match.rules_for(db, user_id)
    hidden = db.scalar(select(Job.id).where(Job.domain_match == domain_match.NOT_MATCHING).limit(1)) is not None
    return {**prefs, "server_enabled": rules.enabled or not prefs["enabled"],
            "derived": {"roles": own["roles"], "skills": own["skills"], "role_words": sorted(rules.role_words)},
            # Excludes the account's own roles or skills cancel out (never applied).
            "overridden": sorted(set(map(domain_match._norm, prefs["exclude_terms"] + prefs["skill_excludes"]))
                                 - set(rules.domain_excludes) - set(rules.skill_excludes)),
            "defaults": {"exclude_terms": list(domain_match.DEFAULT_DOMAIN_EXCLUDES),
                         "skill_excludes": list(domain_match.DEFAULT_SKILL_EXCLUDES)},
            "has_hidden_jobs": hidden}


@router.get("/api/settings/domain")
def get_domain(db: Session = Depends(get_user_db)):
    """The domain filter: the account's lists, what its profile and resume imply, and the learned rules."""
    return _domain_view(db)


@router.put("/api/settings/domain")
def put_domain(payload: DomainIn, db: Session = Depends(get_user_db)):
    _save_domain(db, enabled=payload.enabled, exclude_terms=_clean(payload.exclude_terms),
                 skill_excludes=_clean(payload.skill_excludes), include_terms=_clean(payload.include_terms),
                 learn_from_feedback=payload.learn_from_feedback)
    db.commit()
    return _domain_view(db)


@router.put("/api/settings/domain/learned")
def learned_rule(payload: LearnedIn, db: Session = Depends(get_user_db)):
    """Disable, enable, delete or promote (to a permanent exclude term) one learned rule."""
    prefs = domain_match.preferences(db, _user(db))
    learned = [dict(e) for e in prefs["learned"]]
    entry = next((e for e in learned if e.get("term") == payload.term and e.get("kind") == payload.kind), None)
    if entry is None:
        raise HTTPException(404, "No such learned rule")
    changes: dict = {}
    if payload.action in ("disable", "enable"):
        entry["status"] = "disabled" if payload.action == "disable" else "active"
        entry["user_disabled"] = payload.action == "disable"
    else:
        learned.remove(entry)
        if payload.action == "promote" and payload.kind == "title":
            changes["exclude_terms"] = _clean(prefs["exclude_terms"] + [payload.term])
        elif payload.action == "promote":
            from app.models import CandidateProfile

            profile = db.scalar(select(CandidateProfile))
            if profile is not None:
                search = copy.deepcopy(profile.search_preferences or {})
                search["exclude_companies"] = list(dict.fromkeys([*search.get("exclude_companies", []), entry["term"]]))
                profile.search_preferences = search
        elif payload.action == "delete":
            # Forget it for good: a rule deleted by the person is not learned again from the same events.
            entry["status"], entry["user_disabled"], entry["forgotten"] = "disabled", True, True
            learned.append(entry)
    _save_domain(db, learned=learned, **changes)
    db.commit()
    return _domain_view(db)


@router.post("/api/jobs/domain/reapply")
def reapply(db: Session = Depends(get_user_db)):
    """Assess the account's unranked jobs again with the current lists. Kept jobs are never touched."""
    out = domain_match.reapply(db, _user(db), actor=f"user:{_user(db)}")
    db.commit()
    return out


def _keep(db: Session, ids: list[int]) -> dict:
    kept, skipped = [], []
    for job in db.scalars(select(Job).where(Job.id.in_(ids))).all():
        if job.domain_match != domain_match.NOT_MATCHING:
            skipped.append({"id": job.id, "reason": "This job is not hidden"})
            continue
        domain_match.keep(db, job, actor=f"user:{_user(db)}")
        feedback_learning.record(db, job, "KEPT")
        kept.append(job.id)
    if kept:
        feedback_learning.after_feedback(db, _user(db))
    db.commit()
    return {"kept": kept, "skipped": skipped}


@router.post("/api/jobs/{job_id}/domain-keep")
def keep_job(job_id: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Keep a job the domain filter hid: it goes through recovery, parsing and ranking like any new job."""
    if db.get(Job, job_id) is None:
        raise HTTPException(404, "Job not found")
    out = _keep(db, [job_id])
    run_soon(background, request.app, "ranking")
    return out


@router.post("/api/jobs/bulk-domain-keep")
def bulk_keep(payload: IdsIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    out = _keep(db, payload.ids)
    run_soon(background, request.app, "ranking")
    return out


# ───────────── the resume per job ─────────────

ResumeMode = Literal["auto", "active", "fixed"]


class ResumeChoiceIn(BaseModel):
    # auto: the best-tagged resume (app.services.resume_matching); active: always the active resume; fixed: resume_id.
    # Without a mode, a resume_id means fixed and none means active (the first version of this API).
    mode: ResumeMode | None = None
    resume_id: int | None = None


class BulkResumeIn(ResumeChoiceIn):
    ids: list[int] = Field(min_length=1, max_length=5000)


class TagsIn(BaseModel):
    domain: str | None = Field(default=None, max_length=80)
    roles: list[str] = Field(default_factory=list, max_length=30)


def _resume(db: Session, resume_id: int | None) -> Resume | None:
    if resume_id is None:
        return None
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "Resume not found")
    return resume


def _mode(payload: ResumeChoiceIn) -> ResumeMode:
    return payload.mode or ("fixed" if payload.resume_id is not None else "active")


def _apply_choice(db: Session, job: Job, mode: ResumeMode, resume: Resume | None) -> None:
    if mode == "fixed":
        job.resume_id, job.resume_choice = resume.id, resume_matching.USER
    elif mode == "active":
        job.resume_id, job.resume_choice = None, resume_matching.ACTIVE
    else:
        job.resume_choice = None
        resume_matching.auto_assign(db, job)


def _set_resume(db: Session, jobs: list[Job], mode: ResumeMode, resume: Resume | None) -> dict:
    """Point jobs at a resume. A ranked job is ranked again with it; a tailored packet needs re-tailoring."""
    from app.services.workflow import NEEDS_RETAILOR

    if mode == "fixed" and resume is None:
        raise HTTPException(422, "Choose a resume")
    changed, requeued, skipped = [], [], []
    policy = None
    for job in jobs:
        before = job.resume_id
        _apply_choice(db, job, mode, resume)
        if job.resume_id == before:
            continue
        changed.append(job.id)
        if job.status in ("tailored", "pending_approval"):
            job.status = NEEDS_RETAILOR
        if job.fit_score is not None or job.state in (ranker.S.RANKED, ranker.S.QUALIFIED, ranker.S.RANK_PENDING):
            policy = policy or ranker.policy_for(db, job.user_id)
            why = ranker.queue_job(db, job, policy=policy, actor=f"user:{_user(db)}", reason="resume_changed",
                                   force=True)
            (skipped.append({"id": job.id, "reason": why}) if why else requeued.append(job.id))
    db.commit()
    return {"changed": changed, "ranking_queued": requeued, "ranking_skipped": skipped, "mode": mode,
            "resume_id": resume.id if resume is not None else None}


@router.put("/api/jobs/{job_id}/resume")
def set_job_resume(job_id: int, payload: ResumeChoiceIn, request: Request, background: BackgroundTasks,
                   db: Session = Depends(get_user_db)):
    """Choose the resume this job is ranked and tailored with: auto (best-tagged), active, or a specific one."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    out = _set_resume(db, [job], _mode(payload), _resume(db, payload.resume_id))
    run_soon(background, request.app, "ranking")
    return out


@router.post("/api/jobs/bulk-resume")
def bulk_resume(payload: BulkResumeIn, request: Request, background: BackgroundTasks,
                db: Session = Depends(get_user_db)):
    """Choose the resume for many jobs at once — for example older jobs discovered before setups chose one."""
    resume = _resume(db, payload.resume_id)
    out = _set_resume(db, list(db.scalars(select(Job).where(Job.id.in_(payload.ids))).all()), _mode(payload), resume)
    run_soon(background, request.app, "ranking")
    return out


@router.post("/api/jobs/resume/rematch")
def rematch(request: Request, background: BackgroundTasks, dry_run: bool = False,
            db: Session = Depends(get_user_db)):
    """Match every job that has no pinned resume to the best-tagged resume again (after tagging resumes). Jobs whose
    resume changes and were ranked are ranked again, so `dry_run` first says how many that is."""
    jobs = list(db.scalars(select(Job).where(Job.resume_choice.is_(None) | (Job.resume_choice == resume_matching.AUTO),
                                             Job.status.not_in(("approved", "applied")),
                                             Job.domain_match.is_distinct_from(domain_match.NOT_MATCHING))).all())
    resumes = resume_matching.tagged_resumes(db, _user(db))
    moves = [(job, (match.resume_id if (match := resume_matching.pick(resumes, job)) else None)) for job in jobs]
    moving = [job for job, target in moves if target != job.resume_id]
    reranked = sum(1 for job in moving if job.fit_score is not None)
    if dry_run:
        db.rollback()
        return {"jobs": len(jobs), "changing": len(moving), "reranked": reranked}
    out = _set_resume(db, moving, "auto", None)
    run_soon(background, request.app, "ranking")
    return {"jobs": len(jobs), "changing": len(moving), "reranked": len(out["ranking_queued"]), **out}


def _choice(r: Resume) -> dict:
    return {"id": r.id, "name": r.name or f"Version {r.version}", "version": r.version, "is_active": r.is_active,
            "verification_status": r.verification_status, "domain": r.domain, "roles": r.roles or [],
            "created_at": r.created_at.isoformat() if r.created_at else None}


@router.get("/api/resumes/choices")
def resume_choices(db: Session = Depends(get_user_db)):
    """Every resume version the account can pick for a search or a job, newest first, with its tags."""
    rows = db.scalars(select(Resume).order_by(Resume.version.desc())).all()
    return {"items": [_choice(r) for r in rows], "catalog": resume_matching.catalog()}


@router.put("/api/resumes/{resume_id}")
def rename_resume(resume_id: int, payload: RenameIn, db: Session = Depends(get_user_db)):
    resume = _resume(db, resume_id)
    resume.name = payload.name.strip()
    db.commit()
    return {"id": resume.id, "name": resume.name}


def _clean_tags(payload: TagsIn) -> tuple[str | None, list[str]]:
    domain = (payload.domain or "").strip()[:80] or None
    roles = list(dict.fromkeys(r.strip()[:120] for r in payload.roles if r and r.strip()))[:30]
    return domain, roles


@router.put("/api/resumes/{resume_id}/tags")
def tag_resume(resume_id: int, payload: TagsIn, db: Session = Depends(get_user_db)):
    """Tag a resume version with its domain and target roles. New jobs are matched to the best-tagged resume;
    existing jobs change only when re-matched (POST /api/jobs/resume/rematch)."""
    resume = _resume(db, resume_id)
    resume.domain, resume.roles = _clean_tags(payload)
    db.commit()
    return _choice(resume)


@router.put("/api/profile/resumes/{document_id}/tags")
def tag_resume_file(document_id: int, payload: TagsIn, db: Session = Depends(get_user_db)):
    """Tag an uploaded file: the tags live on its resume version, so the library and Versions always agree."""
    from app.models import ResumeDocument
    from app.services import resume_verification as rv
    from app.services.user_settings import profile_for

    document = db.get(ResumeDocument, document_id)
    if document is None:
        raise HTTPException(404, "Resume not found")
    resume = rv.version_of_document(db, profile_for(db), document)
    resume.domain, resume.roles = _clean_tags(payload)
    db.commit()
    return {"document_id": document.id, **_choice(resume)}
