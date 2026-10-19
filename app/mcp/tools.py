"""The Job Hunt OS operations Claude Desktop may call. Each handler takes a session that belongs to one account.

Handlers only read and write through the application services. They take IDs, filters and structured results,
never SQL, file paths, shell input or settings, and they never return API keys or environment values. The only
paths they return are the packet files get_job_packet writes under MCP_PACKET_DIR.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import desc, nulls_last, select
from sqlalchemy.orm import Session

from app.models import ConversationAction, Evidence, Job, JobRanking, ResumeVariant
from app.prompts.common import prompt_profile
from app.prompts.mcp_application import APPLICATION_RULES, MCP_APPLICATION_PROMPT_VERSION
from app.prompts.mcp_review import MCP_REVIEW_PROMPT_VERSION, REVIEW_RULES
from app.prompts.mcp_tailoring import MCP_TAILORING_PROMPT_VERSION, TAILORING_RULES
from app.prompts.tailoring import JOB_FIELDS, fit_analysis_input
from app.services import apply_queue, conversation_actions, knowledge
from app.services import resume_verification as rv
from app.services.context_builder import ContextError
from app.services.ranker import policy_for, relevant_evidence
from app.services.resume_format import parse_resume
from app.services.review_results import ReviewIn, apply_review, latest_variant
from app.services.tailor_tasks import packet_outcome
from app.services.tailoring_results import (
    TailoredResumeIn,
    enforce_length,
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


def _fit_analysis(db: Session, job: Job) -> dict[str, Any]:
    """The Ranker's completed analysis as the tailoring component reads it: present fields only, no scores, never
    an empty template. Raises ContextError(RANKING_REQUIRED) when the job has no completed analysis."""
    from app.services.context_builder import completed_fit_analysis

    return fit_analysis_input(completed_fit_analysis(db, job))


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


def _posting_targets(db: Session, profile: Any, job: Job, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """What the posting asks for, in the shape the backend Tailor reads it: the parsed requirements (when the
    posting has been parsed), the Ranker's matched requirements with the evidence that proves them, and the ATS
    keywords split into those the candidate's facts support and those that stay gaps."""
    from app.services import job_context as jc
    from app.services.context_builder import keyword_targets, requirement_map

    parsed = jc.build_tailoring_job_context(db, job)
    sources = [{"id": e["id"], "text": e.get("claim") or ""} for e in evidence]
    out = {"job_requirements": {k: v for k, v in parsed.to_dict().items() if k in JOB_FIELDS and v}
           if parsed.structured else {},
           # Only evidence this response carries can be named as proof, and only it can support a keyword.
           "requirement_map": requirement_map(job.fit_analysis, {e["id"] for e in evidence}),
           "keyword_targets": keyword_targets(job, [rv.for_job(db, profile, job).master_resume or "",
                                                   *(" ".join([e.get("claim") or "", *(e.get("skills") or [])])
                                                     for e in evidence)], sources)}
    return {k: v for k, v in out.items() if v}


def _profile_payload(db: Session) -> tuple[Any, dict[str, Any]]:
    profile = get_profile(db)
    return profile, prompt_profile(profile, include_name=True)  # facts about the person, not search settings


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
    active = rv.selected(db, profile)  # the active resume, in the form the candidate policy's resume source names
    _require_resume(active)
    parsed, plan = parse_resume(active.master_resume), _length_plan(db, active)
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
        try:
            fit = _fit_analysis(db, job)
        except ContextError as exc:  # the Tailor never works without the Ranker's analysis
            jobs.append({"job_id": job_id, "error": str(exc)})
            continue
        evidence = _evidence_for(db, job, ranking.relevant_evidence_ids if ranking else [])
        jobs.append({
            "job_id": job.id, "company": job.company, "role": job.title, "location": job.location,
            "job_description": (job.description or "")[:DESCRIPTION_LIMIT],
            "fit_analysis": fit,
            "relevant_evidence": evidence,
            **_posting_targets(db, profile, job, evidence),
            "recommended_resume_focus": (ranking.recommended_resume_focus if ranking
                                         else analysis.get("recommended_resume_focus") or []),
            "resume_gaps": ranking.resume_gaps if ranking else analysis.get("resume_gaps") or [],
            **_retailor_feedback(db, job),
            **_job_resume(db, profile, job),
        })
    # The profile and the active resume are the same for most jobs, so they are sent once; a job that uses another
    # resume (chosen for its search, or by the person) carries its own `resume`, which replaces active_resume for it.
    return {"ok": True, "prompt_version": MCP_TAILORING_PROMPT_VERSION, "rules": list(TAILORING_RULES),
            "candidate_profile": facts,
            "active_resume": {"text": active.master_resume,
                              "resume_id": getattr(getattr(active, "chosen_resume", None), "id", None),
                              "source": getattr(active, "resume_source", "master_text"),
                              "length_plan": plan, "roles": _roles(parsed, plan)},
            "jobs": jobs}


def _length_plan(db: Session, view: Any) -> dict[str, Any]:
    """The account's default length plan for this resume's roles (app.services.resume_length), as the Tailor reads
    it: the page target, the summary's sentence budget and each role's max_bullets by role_index."""
    from app.services import resume_length

    plan = resume_length.plan_for(db, getattr(view, "user_id", None))
    caps = resume_length.role_caps(plan, parse_resume(view.master_resume or "").roles)
    return {**plan.as_dict(), "target": resume_length.PAGE_LABELS[plan.pages],
            "summary_sentences": resume_length.SUMMARY_SENTENCES[plan.pages],
            "role_caps": {str(index): cap for index, cap in caps.items()}}


def _roles(parsed: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role_index": i, "title": r.title, "company": r.company, "dates": r.dates, "bullets": r.bullets,
             "max_bullets": plan["role_caps"].get(str(i), 0)} for i, r in enumerate(parsed.roles)]


def _job_resume(db: Session, profile: Any, job: Job) -> dict[str, Any]:
    view = rv.for_job(db, profile, job)
    chosen = getattr(view, "chosen_resume", None)
    if chosen is None or chosen.is_active:
        return {}  # the job uses the active resume, sent once as active_resume
    parsed, plan = parse_resume(view.master_resume), _length_plan(db, view)
    return {"resume": {"resume_id": chosen.id, "name": chosen.name or f"Version {chosen.version}",
                       "note": "Tailor this job from this resume, not active_resume",
                       "text": view.master_resume, "length_plan": plan, "roles": _roles(parsed, plan)}}


def _retailor_feedback(db: Session, job: Job) -> dict[str, Any]:
    """For a packet review sent back: what to fix this time. Empty for a job that has never been tailored."""
    from app.services.review_results import retailor_feedback

    return retailor_feedback(latest_variant(db, job.id) if job.status == NEEDS_RETAILOR else None)


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
    view = rv.for_job(db, profile, job)
    issues = validate_tailored_resume(db, view, job, data)
    if issues:
        return {"job_id": job.id, "status": "REJECTED", "errors": issues}
    # The length plan is enforced, not rejected: bullets beyond a role's budget are dropped and reported.
    plan = _length_plan(db, view)
    sections, trimmed = enforce_length(list(data.experience_sections), plan)
    data = data.model_copy(update={"experience_sections": sections})
    content = {**to_variant_content(data, source="claude_desktop"), "prompt_version": MCP_TAILORING_PROMPT_VERSION,
               "length_plan": plan}
    variant = persist_tailored_resume(db, job, content)
    return {"job_id": job.id, "status": "SAVED", "resume_id": variant.id, "version": variant.version,
            "job_status": job.status, "next": "waiting for review", **({"trimmed": trimmed} if trimmed else {})}


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


# ───────────── applying ─────────────

def application_page_limit() -> int:
    return get_settings().mcp_application_page_limit


def _threshold(reviewer_score_above: float | None) -> float:
    try:
        return apply_queue.check_threshold(reviewer_score_above)
    except (TypeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _job(db: Session, job_id: Any) -> Job:
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise ToolError("job_id must be an integer")
    job = db.get(Job, job_id)
    if job is None:
        raise ToolError(f"Job {job_id} not found")
    return job


def _blocker_out(blocker: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if not blocker else {k: blocker.get(k) for k in ("reason", "detail", "question_ids", "recorded_at")}


def list_jobs_for_application(db: Session, reviewer_score_above: float | None = None, company: str | None = None,
                              include_blocked: bool = False, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    limit = _check_limit(limit, application_page_limit(), "MCP_APPLICATION_PAGE_LIMIT")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ToolError("offset must be a non-negative integer")
    threshold = _threshold(reviewer_score_above)
    ready = apply_queue.ready_to_apply(db, reviewer_score_above=threshold, include_blocked=bool(include_blocked),
                                       company=company)
    page = ready[offset:offset + limit]
    jobs = [{"job_id": r["job"].id, "title": r["job"].title, "company": r["job"].company,
             "location": r["job"].location, "apply_url": apply_queue.apply_url(r["job"]),
             "posting_url": r["job"].url or r["job"].source_url or None,
             "reviewer_score": r["resume"].reviewer_score, "fit_score": r["job"].fit_score,
             "resume_version": r["resume"].version, "approved_at": _iso(r["application"].approved_at),
             "blocker": _blocker_out(r["blocker"])} for r in page]
    next_offset = offset + len(page) if offset + len(page) < len(ready) else None
    return {"ok": True, "reviewer_score_above": threshold, "total": len(ready), "offset": offset,
            "count": len(jobs), "next_offset": next_offset, "jobs": jobs}


def get_job_packet(db: Session, job_id: int, formats: list[str] | None = None,
                   include_content: bool = False) -> dict[str, Any]:
    job = _job(db, job_id)
    formats = list(dict.fromkeys(formats or apply_queue.FILE_FORMATS))
    unknown = [f for f in formats if f not in apply_queue.FILE_FORMATS]
    if unknown:
        raise ToolError(f"formats must be chosen from {', '.join(apply_queue.FILE_FORMATS)}")
    reason = apply_queue.not_ready_reason(db, job)
    if reason:
        raise ToolError(f"Job {job.id} is not ready to apply to: {reason}")
    application = job.application
    resume = apply_queue.approved_resume(db, application)
    content = resume.content or {}
    return {"ok": True, "prompt_version": MCP_APPLICATION_PROMPT_VERSION, "rules": list(APPLICATION_RULES),
            "job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
            "apply_url": apply_queue.apply_url(job),
            "resume": {"resume_id": resume.id, "version": resume.version, "reviewer_score": resume.reviewer_score,
                       "approved_at": _iso(application.approved_at), "markdown": resume.markdown,
                       "summary": content.get("summary", "")},
            "cover_letter": resume.cover_letter or "",
            "screening_answer_drafts": content.get("screening_answer_drafts") or {},
            "files": apply_queue.packet_files(db, job, resume, formats, include_content=bool(include_content)),
            "blocker": _blocker_out(apply_queue.open_blocker(db, application))}


def search_application_knowledge(db: Session, question: str, limit: int = 5) -> dict[str, Any]:
    if not isinstance(question, str) or len(question.strip()) < 2:
        raise ToolError("question must be the form question's text")
    limit = _check_limit(limit, 20, "at most 20 sources")
    return {"ok": True, "question": question.strip()[:2000], **knowledge.search(db, question[:2000], limit)}


def request_human_answer(db: Session, job_id: int, question: str, answer_options: list[str] | None = None,
                         answer_type: str | None = None, category: str = "general", sensitive: bool = False,
                         required: bool | None = None, field_label: str | None = None,
                         note: str | None = None) -> dict[str, Any]:
    job = _job(db, job_id)
    try:
        saved = knowledge.request_answer(db, job, question, answer_options=answer_options, answer_type=answer_type,
                                         category=category, sensitive=sensitive, required=required,
                                         field_label=field_label, note=note)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "job_id": job.id, **saved}


def record_application_result(db: Session, job_id: int, outcome: str, confirmation_id: str | None = None,
                              blocker_reason: str | None = None, detail: str | None = None,
                              question_ids: list[int] | None = None) -> dict[str, Any]:
    job = _job(db, job_id)
    if outcome not in ("submitted", "blocked"):
        raise ToolError("outcome must be submitted or blocked")
    if question_ids is not None and not (isinstance(question_ids, list)
                                         and all(isinstance(i, int) and not isinstance(i, bool) for i in question_ids)):
        raise ToolError("question_ids must be a list of integers")
    application = apply_queue.lock_application(db, job)
    if application is not None and application.status == "applied":
        # Idempotent: a second report of the same submission changes nothing.
        return {"ok": True, "status": "ALREADY_APPLIED", "job_id": job.id, "job_status": job.status,
                "applied_at": _iso(application.applied_at), "confirmation_id": application.confirmation_id,
                "submitted_via": application.submitted_via}
    if application is None or application.status != "approved" or job.status != "approved":
        raise ToolError(f"Job {job.id} is {job.status}, not approved for application")
    if outcome == "blocked":
        try:
            blocker = apply_queue.record_blocker(db, job, blocker_reason or "", detail=detail or "",
                                                 question_ids=question_ids)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {"ok": True, "status": "BLOCKED", "job_id": job.id, "job_status": job.status,
                "blocker": _blocker_out(blocker)}
    duplicate = apply_queue.applied_duplicate(db, job)
    if duplicate is not None:
        db.rollback()
        return {"ok": False, "status": "DUPLICATE_APPLICATION", "job_id": job.id, "duplicate_of_job_id": duplicate.id,
                "error": f"Already applied to the same posting as job {duplicate.id} ({duplicate.company}); "
                         "not recorded"}
    application = apply_queue.record_submission(db, job, confirmation_id=confirmation_id)
    return {"ok": True, "status": "SUBMITTED", "job_id": job.id, "job_status": job.status,
            "applied_at": _iso(application.applied_at), "confirmation_id": application.confirmation_id,
            "followup_due_at": _iso(application.followup_due_at)}
