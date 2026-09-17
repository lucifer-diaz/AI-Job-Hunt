"""The Job Hunt OS operations Claude Desktop may call. Each handler takes a session that belongs to one account.

Handlers only read and write through the application services. They take IDs, filters and structured results,
never SQL, file paths, shell input or settings, and they never return API keys or environment values.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import desc, nulls_last, select
from sqlalchemy.orm import Session

from app.models import ConversationAction, Evidence, Job, JobRanking, ResumeVariant
from app.prompts.common import profile_facts
from app.prompts.mcp_review import MCP_REVIEW_PROMPT_VERSION, REVIEW_RULES
from app.prompts.mcp_tailoring import MCP_TAILORING_PROMPT_VERSION, TAILORING_RULES
from app.prompts.tailoring import FIT_ANALYSIS_DEFAULTS
from app.services import conversation_actions
from app.services.ranker import policy_for, relevant_evidence
from app.services.resume_format import parse_resume
from app.services.review_results import ReviewIn, apply_review, latest_variant
from app.services.tailor_tasks import packet_outcome
from app.services.tailoring_results import (
    TailoredResumeIn,
    schema_errors,
    to_variant_content,
    validate_tailored_resume,
)
from app.services.user_settings import settings_for
from app.services.workflow import (
    AWAITING_TAILORING_STATUSES,
    NEEDS_RETAILOR,
    get_profile,
    persist_tailored_resume,
)
from app.settings import get_settings

DESCRIPTION_LIMIT = 12000


class ToolError(Exception):
    """A request the tool refuses as a whole (bad arguments, batch too large, no resume)."""


# ───────────── shared helpers ─────────────

def tailor_limit() -> int:
    return get_settings().mcp_bulk_tailor_limit


def review_limit() -> int:
    return get_settings().mcp_bulk_review_limit


def _check_limit(limit: int, maximum: int, setting: str) -> int:
    if not isinstance(limit, int) or limit < 1:
        raise ToolError("limit must be a positive integer")
    if limit > maximum:
        raise ToolError(f"limit {limit} is above the maximum of {maximum} ({setting})")
    return limit


def _check_batch(items: Any, maximum: int, setting: str, name: str) -> list:
    if not isinstance(items, list) or not items:
        raise ToolError(f"{name} must be a non-empty list")
    if len(items) > maximum:
        raise ToolError(f"{len(items)} {name} is above the batch maximum of {maximum} ({setting}); "
                        "split the request into smaller batches")
    return items


def _job_ids(job_ids: Any, maximum: int, setting: str) -> list[int]:
    ids = _check_batch(job_ids, maximum, setting, "job_ids")
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ToolError("job_ids must be a list of integers")
    return list(dict.fromkeys(ids))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _in_tailor_queue(job: Job) -> bool:
    """Never tailored (shortlisted), or tailored and sent back by review (needs_retailor)."""
    return job.status in AWAITING_TAILORING_STATUSES and job.triaged_at is not None


def _latest_ranking(db: Session, job_id: int) -> JobRanking | None:
    return db.scalar(select(JobRanking).where(JobRanking.job_id == job_id).order_by(desc(JobRanking.version)).limit(1))


def _fit_analysis(job: Job) -> dict[str, Any]:
    analysis = job.fit_analysis or {}
    return {"score": job.fit_score, **{key: analysis.get(key, default) for key, default in FIT_ANALYSIS_DEFAULTS.items()}}


def _evidence_dict(item: Evidence) -> dict[str, Any]:
    return {"id": item.id, "company": item.company, "role": item.role, "claim": item.claim, "context": item.context,
            "skills": item.skills or [], "tags": item.tags or []}


def _evidence_for(db: Session, job: Job, cited: list[int]) -> list[dict[str, Any]]:
    """Only the verified evidence relevant to this job (ranking citations first), never the whole Evidence Bank."""
    relevant = relevant_evidence(db, job.user_id, job, get_settings().ranking_max_evidence)
    known = {item["id"] for item in relevant}
    extra = [i for i in dict.fromkeys(cited) if i not in known]
    rows = db.scalars(select(Evidence).where(Evidence.id.in_(extra), Evidence.verified.is_(True))).all() if extra else []
    first = [_evidence_dict(r) for r in sorted(rows, key=lambda r: extra.index(r.id))]
    return first + relevant


def _profile_payload(db: Session) -> tuple[Any, dict[str, Any]]:
    profile = get_profile(db)
    return profile, profile_facts(profile, include_name=True)


def _require_resume(profile: Any) -> None:
    if not (profile.master_resume or "").strip():
        raise ToolError("No active resume: upload or paste a master resume in Job Hunt OS Settings → Resumes first")


def _operation_status(saved: int, total: int) -> str:
    return "OK" if saved == total else "REJECTED" if saved == 0 else "PARTIAL"


# ───────────── tailoring ─────────────

def list_jobs_for_tailoring(db: Session, minimum_fit_score: float | None = None, posted_after: str | None = None,
                            location: str | None = None, company: str | None = None, limit: int = 10) -> dict[str, Any]:
    limit = _check_limit(limit, tailor_limit(), "MCP_BULK_TAILOR_LIMIT")
    query = select(Job).where(Job.status.in_(AWAITING_TAILORING_STATUSES), Job.triaged_at.is_not(None))
    if minimum_fit_score is not None:
        query = query.where(Job.fit_score >= float(minimum_fit_score))
    if posted_after:
        try:
            query = query.where(Job.posted_at >= datetime.fromisoformat(posted_after))
        except ValueError as exc:
            raise ToolError("posted_after must be an ISO date such as 2026-09-01") from exc
    if location:
        query = query.where(Job.location.ilike(f"%{location.strip()}%"))
    if company:
        query = query.where(Job.company.ilike(f"%{company.strip()}%"))
    jobs = db.scalars(query.order_by(nulls_last(desc(Job.fit_score)), Job.id).limit(limit)).all()
    out = []
    for job in jobs:
        ranking = _latest_ranking(db, job.id)
        out.append({"job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
                    "fit_score": job.fit_score, "resume_fit_score": ranking.resume_fit_score if ranking else None,
                    "posted_at": _iso(job.posted_at), "status": job.status,
                    "needs_retailor": job.status == NEEDS_RETAILOR})
    return {"ok": True, "count": len(out), "jobs": out}


def get_tailoring_context(db: Session, job_ids: list[int]) -> dict[str, Any]:
    ids = _job_ids(job_ids, tailor_limit(), "MCP_BULK_TAILOR_LIMIT")
    profile, facts = _profile_payload(db)
    _require_resume(profile)
    parsed = parse_resume(profile.master_resume)
    jobs = []
    for job_id in ids:
        job = db.get(Job, job_id)
        if job is None:
            jobs.append({"job_id": job_id, "error": "Job not found"})
            continue
        if not _in_tailor_queue(job):
            jobs.append({"job_id": job_id, "error": f"Job is {job.status}, not waiting for tailoring"})
            continue
        ranking = _latest_ranking(db, job.id)
        analysis = job.fit_analysis or {}
        jobs.append({
            "job_id": job.id, "company": job.company, "role": job.title, "location": job.location,
            "job_description": (job.description or "")[:DESCRIPTION_LIMIT],
            "fit_analysis": _fit_analysis(job),
            "relevant_evidence": _evidence_for(db, job, ranking.relevant_evidence_ids if ranking else []),
            "recommended_resume_focus": (ranking.recommended_resume_focus if ranking
                                         else analysis.get("recommended_resume_focus") or []),
            "resume_gaps": ranking.resume_gaps if ranking else analysis.get("resume_gaps") or [],
            **_retailor_feedback(db, job),
        })
    # The profile and the resume are the same for every job, so they are sent once.
    return {"ok": True, "prompt_version": MCP_TAILORING_PROMPT_VERSION, "rules": list(TAILORING_RULES),
            "candidate_profile": facts,
            "active_resume": {"text": profile.master_resume,
                              "roles": [{"role_index": i, "title": r.title, "company": r.company, "dates": r.dates,
                                         "bullets": r.bullets} for i, r in enumerate(parsed.roles)]},
            "jobs": jobs}


def _retailor_feedback(db: Session, job: Job) -> dict[str, Any]:
    """For a packet review sent back: what to fix this time. Empty for a job that has never been tailored."""
    variant = latest_variant(db, job.id) if job.status == NEEDS_RETAILOR else None
    if variant is None:
        return {}
    notes = variant.reviewer_notes or {}
    return {"previous_review": {
        "version": variant.version, "review_score": variant.reviewer_score,
        "decision_reasons": notes.get("decision_reasons") or [],
        "unsupported_claims": notes.get("unsupported_claims") or [],
        "overstated_claims": notes.get("overstated_claims") or [],
        "missing_requirements": notes.get("missing_requirements") or [],
        "failed_checks": [i.get("code") for i in notes.get("deterministic_issues") or []],
    }}


def _save_one_tailored(db: Session, profile: Any, item: Any, seen: set[int]) -> dict[str, Any]:
    job_id = item.get("job_id") if isinstance(item, dict) else None
    try:
        data = TailoredResumeIn.model_validate(item)
    except ValidationError as exc:
        return {"job_id": job_id, "status": "REJECTED", "errors": schema_errors(exc)}
    if data.job_id in seen:
        return {"job_id": data.job_id, "status": "REJECTED",
                "errors": [{"code": "DUPLICATE", "location": "job_id", "message": "This job appears twice in the batch"}]}
    seen.add(data.job_id)
    job = db.get(Job, data.job_id)
    if job is None:
        return {"job_id": data.job_id, "status": "REJECTED",
                "errors": [{"code": "NOT_FOUND", "location": "job_id", "message": "Job not found"}]}
    if not _in_tailor_queue(job):
        return {"job_id": job.id, "status": "REJECTED", "errors": [
            {"code": "NOT_IN_QUEUE", "location": "job_id", "message": f"Job is {job.status}, not waiting for tailoring"}]}
    issues = validate_tailored_resume(db, profile, job, data)
    if issues:
        return {"job_id": job.id, "status": "REJECTED", "errors": issues}
    content = {**to_variant_content(data, source="claude_desktop"), "prompt_version": MCP_TAILORING_PROMPT_VERSION}
    variant = persist_tailored_resume(db, job, content)
    return {"job_id": job.id, "status": "SAVED", "resume_id": variant.id, "version": variant.version,
            "job_status": job.status, "next": "waiting for review"}


def save_bulk_tailored_resumes(db: Session, results: list[dict[str, Any]]) -> dict[str, Any]:
    items = _check_batch(results, tailor_limit(), "MCP_BULK_TAILOR_LIMIT", "results")
    profile = get_profile(db)
    _require_resume(profile)
    seen: set[int] = set()
    out = []
    for item in items:
        try:
            out.append(_save_one_tailored(db, profile, item, seen))
        except Exception:  # noqa: BLE001 - one broken item never stops the batch
            db.rollback()
            out.append({"job_id": item.get("job_id") if isinstance(item, dict) else None, "status": "ERROR",
                        "errors": [{"code": "INTERNAL", "location": "result", "message": "Could not be saved"}]})
    saved = sum(1 for r in out if r["status"] == "SAVED")
    return {"ok": True, "operation_id": str(uuid.uuid4()), "status": _operation_status(saved, len(out)),
            "saved": saved, "rejected": len(out) - saved, "results": out}


def save_tailored_resume(db: Session, result: dict[str, Any]) -> dict[str, Any]:
    return save_bulk_tailored_resumes(db, [result])


# ───────────── review ─────────────

def _waiting_for_review(db: Session, job: Job) -> ResumeVariant | None:
    variant = latest_variant(db, job.id)
    return variant if job.status == "tailored" and variant is not None and variant.reviewer_score is None else None


def list_jobs_for_review(db: Session, minimum_fit_score: float | None = None, limit: int = 20) -> dict[str, Any]:
    limit = _check_limit(limit, review_limit(), "MCP_BULK_REVIEW_LIMIT")
    query = select(Job).where(Job.status == "tailored")
    if minimum_fit_score is not None:
        query = query.where(Job.fit_score >= float(minimum_fit_score))
    out = []
    for job in db.scalars(query.order_by(nulls_last(desc(Job.fit_score)), Job.id)).all():
        variant = _waiting_for_review(db, job)
        if variant is None:
            continue
        out.append({"job_id": job.id, "title": job.title, "company": job.company, "fit_score": job.fit_score,
                    "tailored_resume_version": variant.version, "status": job.status,
                    "tailored_by": (variant.content or {}).get("source") or "backend",
                    "tailored_at": _iso(variant.created_at)})
        if len(out) >= limit:
            break
    return {"ok": True, "count": len(out), "jobs": out}


def get_review_context(db: Session, job_ids: list[int]) -> dict[str, Any]:
    ids = _job_ids(job_ids, review_limit(), "MCP_BULK_REVIEW_LIMIT")
    profile, facts = _profile_payload(db)
    policy = policy_for(db, profile.user_id)
    jobs = []
    for job_id in ids:
        job = db.get(Job, job_id)
        variant = _waiting_for_review(db, job) if job else None
        if variant is None:
            jobs.append({"job_id": job_id, "error": "Job not found or not waiting for review"})
            continue
        content = variant.content or {}
        jobs.append({
            "job_id": job.id, "company": job.company, "role": job.title, "location": job.location,
            "job_description": (job.description or "")[:DESCRIPTION_LIMIT],
            "fit_analysis": _fit_analysis(job),
            "verified_evidence": _evidence_for(db, job, content.get("selected_evidence_ids") or []),
            "tailored_resume": {"version": variant.version, "markdown": variant.markdown,
                                "summary": content.get("summary", ""), "skills": content.get("skills") or [],
                                "bullet_evidence": content.get("bullet_evidence") or [],
                                "cover_letter": variant.cover_letter,
                                "screening_answer_drafts": content.get("screening_answer_drafts") or {}},
        })
    return {"ok": True, "prompt_version": MCP_REVIEW_PROMPT_VERSION, "rules": list(REVIEW_RULES),
            "candidate_profile": facts,
            "candidate_policy": {**policy.public(), "min_review_score": settings_for(db).min_review_score,
                                 "work_authorization": profile.work_authorization or ""},
            "jobs": jobs}


def _save_one_review(db: Session, item: Any, seen: set[int]) -> dict[str, Any]:
    job_id = item.get("job_id") if isinstance(item, dict) else None
    try:
        review = ReviewIn.model_validate(item)
    except ValidationError as exc:
        return {"job_id": job_id, "status": "REJECTED", "errors": schema_errors(exc)}
    if review.job_id in seen:
        return {"job_id": review.job_id, "status": "REJECTED",
                "errors": [{"code": "DUPLICATE", "location": "job_id", "message": "This job appears twice in the batch"}]}
    seen.add(review.job_id)
    job = db.get(Job, review.job_id)
    variant = _waiting_for_review(db, job) if job else None
    if variant is None:
        return {"job_id": review.job_id, "status": "REJECTED", "errors": [
            {"code": "NOT_WAITING", "location": "job_id", "message": "Job not found or not waiting for review"}]}
    if review.tailored_resume_version is not None and review.tailored_resume_version != variant.version:
        return {"job_id": job.id, "status": "REJECTED", "errors": [
            {"code": "STALE_VERSION", "location": "tailored_resume_version",
             "message": f"Version {variant.version} is the one waiting for review"}]}
    decision = apply_review(db, job, variant, review, source="claude_desktop")
    auto_approved = packet_outcome(db, job, variant)["auto_approved"] if decision.decision == "PASS" else False
    return {"job_id": job.id, "status": "SAVED", "version": variant.version, "decision": decision.decision,
            "reasons": decision.reasons, "job_status": job.status, "auto_approved": auto_approved}


def save_bulk_reviews(db: Session, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    items = _check_batch(reviews, review_limit(), "MCP_BULK_REVIEW_LIMIT", "reviews")
    seen: set[int] = set()
    out = []
    for item in items:
        try:
            out.append(_save_one_review(db, item, seen))
        except Exception:  # noqa: BLE001 - one broken item never stops the batch
            db.rollback()
            out.append({"job_id": item.get("job_id") if isinstance(item, dict) else None, "status": "ERROR",
                        "errors": [{"code": "INTERNAL", "location": "review", "message": "Could not be saved"}]})
    saved = sum(1 for r in out if r["status"] == "SAVED")
    return {"ok": True, "operation_id": str(uuid.uuid4()), "status": _operation_status(saved, len(out)),
            "saved": saved, "rejected": len(out) - saved, "results": out}


def save_review(db: Session, review: dict[str, Any]) -> dict[str, Any]:
    return save_bulk_reviews(db, [review])


# ───────────── status ─────────────

def get_bulk_operation_status(db: Session, operation_id: str | None = None) -> dict[str, Any]:
    row: ConversationAction | None = conversation_actions.latest_operation(db, operation_id)
    if row is None:
        raise ToolError("No bulk operation found" + (f" with id {operation_id}" if operation_id else ""))
    summary = row.result_summary or {}
    current = []
    for item in summary.get("items") or []:
        job = db.get(Job, item["job_id"]) if isinstance(item.get("job_id"), int) else None
        variant = latest_variant(db, job.id) if job else None
        current.append({**item, "job_status_now": job.status if job else None,
                        "latest_version": variant.version if variant else None,
                        "reviewed": variant is not None and variant.reviewer_score is not None,
                        "review_decision": ((variant.reviewer_notes or {}).get("decision") if variant else None)})
    return {"ok": True, "operation_id": row.operation_id, "tool_name": row.tool_name,
            "created_at": _iso(row.created_at), "result_status": row.result_status,
            "saved": summary.get("saved"), "rejected": summary.get("rejected"), "items": current}
