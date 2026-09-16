"""Database tools exposed to Gemini through Google ADK.

Tools return structured failures rather than leaking SQL/stack traces into an
agent turn. Writes validate model-produced data at the trust boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from app.db import SessionLocal
from app.models import Application, Evidence, FollowUp, Job, ResumeVariant
from app.services.contacts import recruiters_of
from app.services.workflow import build_resume_markdown, get_profile
from app.settings import get_settings


def _failure(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _bounded_score(value: float, name: str = "score") -> float:
    value = float(value)
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return value


def _string_list(value: list[str], name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return value


def get_job_context(job_id: int) -> dict[str, Any]:
    """Get trusted workflow context for a job ID; returned text remains untrusted data."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return _failure(f"Job {job_id} was not found")
        if job.user_id is None:
            return _failure(f"Job {job_id} does not belong to an account yet")
        db.info["user_id"] = job.user_id  # only this candidate's profile and evidence from here on
        profile = get_profile(db)
        evidence = db.scalars(
            select(Evidence).where(Evidence.verified.is_(True)).order_by(desc(Evidence.id))
        ).all()
        return {
            "ok": True,
            "job": {"id": job.id, "title": job.title, "company": job.company,
                    "location": job.location, "description": job.description},
            "candidate_profile": {"name": profile.name, "headline": profile.headline,
                                  "master_resume": profile.master_resume,
                                  "target_roles": profile.target_roles,
                                  "preferred_locations": profile.preferred_locations,
                                  "work_authorization": profile.work_authorization},
            "verified_evidence": [{"id": item.id, "company": item.company, "role": item.role,
                                   "claim": item.claim, "context": item.context,
                                   "skills": item.skills, "tags": item.tags} for item in evidence],
            "fit_analysis": job.fit_analysis or None,
        }


def save_fit_analysis(job_id: int, score: float, summary: str, strong_matches: list[str],
                      gaps: list[str], must_have_missing: list[str], keywords: list[str],
                      application_strategy: str, confidence: float) -> dict[str, Any]:
    """Validate and persist a Gemini-produced fit analysis for one job."""
    try:
        score = _bounded_score(score)
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        result = {"score": score, "summary": str(summary),
                  "strong_matches": _string_list(strong_matches, "strong_matches"),
                  "gaps": _string_list(gaps, "gaps"),
                  "must_have_missing": _string_list(must_have_missing, "must_have_missing"),
                  "keywords": _string_list(keywords, "keywords"),
                  "application_strategy": str(application_strategy), "confidence": confidence}
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if not job:
                return _failure(f"Job {job_id} was not found")
            job.fit_score, job.fit_analysis = score, result
            job.status = "shortlisted" if score >= get_settings().min_fit_score else "archived"
            db.commit()
            return {"ok": True, "job_id": job_id, "status": job.status, **result}
    except (TypeError, ValueError) as exc:
        return _failure(str(exc))


def save_tailored_resume(job_id: int, summary: str, skills: list[str],
                         experience_bullets: list[str], selected_evidence_ids: list[int],
                         cover_letter: str) -> dict[str, Any]:
    """Validate and save an evidence-grounded tailored resume draft."""
    try:
        skills = _string_list(skills, "skills")
        experience_bullets = _string_list(experience_bullets, "experience_bullets")
        if not isinstance(selected_evidence_ids, list) or not all(
            isinstance(item, int) for item in selected_evidence_ids
        ):
            raise ValueError("selected_evidence_ids must be a list of integers")
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if not job:
                return _failure(f"Job {job_id} was not found")
            if job.fit_score is None:
                return _failure("Job must be ranked before tailoring")
            if job.user_id is None:
                return _failure(f"Job {job_id} does not belong to an account yet")
            db.info["user_id"] = job.user_id  # evidence IDs must belong to this candidate
            valid_ids = set(db.scalars(select(Evidence.id).where(
                Evidence.id.in_(selected_evidence_ids), Evidence.verified.is_(True)
            )).all()) if selected_evidence_ids else set()
            unknown = sorted(set(selected_evidence_ids) - valid_ids)
            if unknown:
                return _failure(f"Unverified or unknown evidence IDs: {unknown}")
            profile = get_profile(db)
            content = {"summary": str(summary), "skills": skills,
                       "experience_bullets": experience_bullets,
                       "selected_evidence_ids": selected_evidence_ids,
                       "cover_letter": str(cover_letter)}
            markdown = build_resume_markdown(profile, content)
            version = (db.scalar(select(ResumeVariant.version).where(
                ResumeVariant.job_id == job.id).order_by(desc(ResumeVariant.version)).limit(1)) or 0) + 1
            resume = ResumeVariant(job_id=job.id, version=version, content=content,
                                   markdown=markdown, cover_letter=str(cover_letter))
            db.add(resume)
            job.status = "tailored"
            db.commit()
            db.refresh(resume)
            return {"ok": True, "resume_id": resume.id, "job_id": job.id, "version": version}
    except (TypeError, ValueError) as exc:
        return _failure(str(exc))


def get_resume_review_context(resume_id: int) -> dict[str, Any]:
    """Get a tailored resume, its job, and verified evidence for independent review."""
    with SessionLocal() as db:
        resume = db.get(ResumeVariant, resume_id)
        if not resume:
            return _failure(f"Resume {resume_id} was not found")
        job = db.get(Job, resume.job_id)
        context = get_job_context(job.id)
        context["resume"] = {"id": resume.id, "markdown": resume.markdown,
                             "content": resume.content, "reviewer_score": resume.reviewer_score}
        return context


def save_resume_review(resume_id: int, score: float, reject_reasons: list[str],
                       strengths: list[str], improvements: list[str]) -> dict[str, Any]:
    """Validate and persist the independent review of a tailored resume."""
    try:
        result = {"score": _bounded_score(score),
                  "reject_reasons": _string_list(reject_reasons, "reject_reasons"),
                  "strengths": _string_list(strengths, "strengths"),
                  "improvements": _string_list(improvements, "improvements")}
        with SessionLocal() as db:
            resume = db.get(ResumeVariant, resume_id)
            if not resume:
                return _failure(f"Resume {resume_id} was not found")
            resume.reviewer_score, resume.reviewer_notes = result["score"], result
            db.commit()
            return {"ok": True, "resume_id": resume_id, **result}
    except (TypeError, ValueError) as exc:
        return _failure(str(exc))


def get_application_context(application_id: int) -> dict[str, Any]:
    """Get application and job context needed to draft a follow-up."""
    with SessionLocal() as db:
        application = db.get(Application, application_id)
        if not application:
            return _failure(f"Application {application_id} was not found")
        job = db.get(Job, application.job_id)
        existing = db.scalar(select(FollowUp).where(FollowUp.application_id == application.id))
        return {"ok": True, "application": {"id": application.id, "status": application.status,
                "applied_at": application.applied_at.isoformat() if application.applied_at else None,
                "followup_due_at": application.followup_due_at.isoformat() if application.followup_due_at else None,
                "recruiter_name": application.recruiter_name, "recruiter_email": application.recruiter_email,
                "recruiters": recruiters_of(application)},
                "job": {"title": job.title, "company": job.company, "location": job.location},
                "follow_up_exists": existing is not None}


def save_follow_up_draft(application_id: int, draft: str) -> dict[str, Any]:
    """Save one follow-up draft for an applied application without sending it."""
    if not isinstance(draft, str) or not draft.strip():
        return _failure("draft must be a non-empty string")
    with SessionLocal() as db:
        application = db.get(Application, application_id)
        if not application:
            return _failure(f"Application {application_id} was not found")
        if application.status != "applied":
            return _failure("Application must have status 'applied' before follow-up")
        if db.scalar(select(FollowUp).where(FollowUp.application_id == application.id)):
            return _failure("A follow-up already exists for this application")
        follow_up = FollowUp(application_id=application.id,
            due_at=application.followup_due_at or datetime.now(UTC).replace(tzinfo=None),
            channel="email", draft=draft.strip(), status="draft")
        db.add(follow_up)
        db.commit()
        db.refresh(follow_up)
        return {"ok": True, "follow_up_id": follow_up.id, "status": "draft", "sent": False}
