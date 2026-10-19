"""Approved packets waiting to be submitted, and what happened when someone tried.

A job is ready to apply to when its packet is approved, nothing was submitted for it yet, the approved resume's
reviewer score is above the floor, it has an application URL, no other posting of the same job was already applied to, and its last attempt
did not stop at a blocker that is still open. Recording a submission goes through `workflow.mark_applied`, the
same path the Apply screen uses, so a job is only ever applied to once.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, nulls_last, or_, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent, Job, ResumeVariant, User
from app.services import approval, knowledge
from app.services.exports import DOCX_MEDIA_TYPE, markdown_to_docx, markdown_to_simple_pdf, packet_filename, packet_slug
from app.services.workflow import get_profile, mark_applied
from app.settings import get_settings

# The approved resume must have been scored above this by the reviewer. A caller may only raise it.
MIN_REVIEWER_SCORE = 60.0
BLOCKER_REASONS = ("captcha", "login_required", "account_required", "needs_human_answer", "missing_document",
                   "posting_closed", "form_error", "other")
FILE_FORMATS = ("pdf", "docx")
MEDIA_TYPES = {"pdf": "application/pdf", "docx": DOCX_MEDIA_TYPE}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def apply_url(job: Job) -> str | None:
    """Where to apply: the employer's own form when the board gave one, else the posting."""
    return job.direct_apply_url or job.url or job.canonical_apply_url or job.source_url or None


def approved_resume(db: Session, application: Application | None) -> ResumeVariant | None:
    return db.get(ResumeVariant, application.approved_resume_id) if application and application.approved_resume_id else None


def applied_duplicate(db: Session, job: Job) -> Job | None:
    """Another saved posting of the same job (same apply URL, posting URL or company/title/city) already applied to."""
    same = [column == value for column, value in ((Job.canonical_apply_url, job.canonical_apply_url),
                                                  (Job.canonical_url, job.canonical_url),
                                                  (Job.dedupe_key, job.dedupe_key)) if value]
    if not same:
        return None
    return db.scalar(select(Job).join(Application, Application.job_id == Job.id)
                     .where(Job.id != job.id, Application.status == "applied", or_(*same)).order_by(Job.id).limit(1))


def open_blocker(db: Session, application: Application) -> dict[str, Any] | None:
    """The blocker the last attempt stopped at, unless it has since been cleared.

    A `needs_human_answer` blocker clears itself once a person has answered every question it names.
    """
    blocker = application.apply_blocker
    if not blocker:
        return None
    if blocker.get("reason") == "needs_human_answer" and blocker.get("question_ids") \
            and knowledge.answered(db, blocker["question_ids"]):
        return None
    return blocker


def check_threshold(reviewer_score_above: float | None) -> float:
    threshold = MIN_REVIEWER_SCORE if reviewer_score_above is None else float(reviewer_score_above)
    if threshold < MIN_REVIEWER_SCORE:
        raise ValueError(f"reviewer_score_above cannot be lower than {MIN_REVIEWER_SCORE:g}")
    return threshold


def ready_to_apply(db: Session, *, reviewer_score_above: float | None = None, include_blocked: bool = False,
                   company: str | None = None) -> list[dict[str, Any]]:
    """Every approved, unsubmitted job above the reviewer floor, best reviewed first."""
    threshold = check_threshold(reviewer_score_above)
    query = (select(Job, Application, ResumeVariant)
             .join(Application, Application.job_id == Job.id)
             .join(ResumeVariant, ResumeVariant.id == Application.approved_resume_id)
             .where(Job.status == "approved", Application.status == "approved", Application.applied_at.is_(None),
                    ResumeVariant.reviewer_score > threshold))
    if company:
        query = query.where(Job.company.ilike(f"%{company.strip()}%"))
    query = query.order_by(desc(ResumeVariant.reviewer_score), nulls_last(desc(Job.fit_score)), Job.id)
    out = []
    for job, application, resume in db.execute(query).all():
        if not apply_url(job) or applied_duplicate(db, job) is not None:
            continue
        if not approval.verify(db, application).ok:
            continue  # changed after approval: it needs a new approval before anyone applies with it
        blocker = open_blocker(db, application)
        if blocker and not include_blocked:
            continue
        out.append({"job": job, "application": application, "resume": resume, "blocker": blocker})
    return out


def not_ready_reason(db: Session, job: Job, *, reviewer_score_above: float | None = None) -> str | None:
    """Why this job's packet may not be used for an application now, or None when it may."""
    application = job.application
    if application is not None and application.status == "applied":
        return f"Already applied on {application.applied_at:%Y-%m-%d}" if application.applied_at else "Already applied"
    if application is None or application.status != "approved" or job.status != "approved":
        return f"Job is {job.status}, not approved for application"
    resume = approved_resume(db, application)
    if resume is None or resume.reviewer_score is None:
        return "The approved packet has no reviewed resume"
    checked = approval.verify(db, application)
    if not checked.ok:
        return checked.reason
    threshold = check_threshold(reviewer_score_above)
    if resume.reviewer_score <= threshold:
        return f"Reviewer score {resume.reviewer_score:g} is not above {threshold:g}"
    duplicate = applied_duplicate(db, job)
    if duplicate is not None:
        return f"Already applied to the same posting as job {duplicate.id} ({duplicate.company})"
    if not apply_url(job):
        return "The job has no application URL; add the posting URL in Job Hunt OS first"
    return None


def packet_files(db: Session, job: Job, resume: ResumeVariant, formats: list[str], *,
                 include_content: bool = False) -> list[dict[str, Any]]:
    """Render the approved resume and write it where a browser upload can pick it up.

    Files go to MCP_PACKET_DIR/<account>/<job>/ under the same names the Apply screen downloads.
    """
    renderers = {"pdf": markdown_to_simple_pdf, "docx": markdown_to_docx}
    slug = packet_slug(get_profile(db).name, job.company)
    folder = Path(get_settings().mcp_packet_dir).expanduser().resolve() / str(job.user_id) / str(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for kind in formats:
        data = renderers[kind](resume.markdown)
        path = folder / packet_filename(slug, kind, resume.version)
        path.write_bytes(data)
        item = {"format": kind, "filename": path.name, "path": str(path), "media_type": MEDIA_TYPES[kind],
                "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if include_content:
            item["content_base64"] = base64.b64encode(data).decode()
        out.append(item)
    return out


def record_blocker(db: Session, job: Job, reason: str, *, detail: str = "", question_ids: list[int] | None = None,
                   recorded_by: str = "claude_desktop") -> dict[str, Any]:
    if reason not in BLOCKER_REASONS:
        raise ValueError(f"blocker_reason must be one of {', '.join(BLOCKER_REASONS)}")
    question_ids = list(dict.fromkeys(question_ids or []))
    if reason == "needs_human_answer" and not question_ids:
        raise ValueError("needs_human_answer needs the question_ids request_human_answer returned")
    blocker = {"reason": reason, "detail": (detail or "").strip()[:2000] or None, "question_ids": question_ids,
               "recorded_by": recorded_by, "recorded_at": _now().isoformat()}
    application = job.application
    application.apply_blocker = blocker
    db.add(ApplicationEvent(application=application, kind="blocker", title=f"Application blocked: {reason.replace('_', ' ')}",
                            detail=blocker["detail"] or "", meta={k: blocker[k] for k in ("reason", "question_ids")}))
    db.commit()
    return blocker


def record_submission(db: Session, job: Job, *, confirmation_id: str | None = None,
                      submitted_via: str = "claude_desktop") -> Application:
    confirmation_id = (confirmation_id or "").strip()[:200] or None
    return mark_applied(db, job, submitted_by="Claude Desktop", submitted_via=submitted_via,
                        confirmation_id=confirmation_id)


def lock_application(db: Session, job: Job) -> Application | None:
    """Re-read the application under row locks, so two agents cannot both record a submission.

    The account row is locked first: duplicate postings are different jobs, so a lock on this job's application
    alone would let two sessions each pass the duplicate check before either commits. Submissions for one account
    are therefore recorded one at a time (SQLite, which has no row locks, serialises writes anyway).
    """
    db.execute(select(User.id).where(User.id == job.user_id).with_for_update())
    return db.scalar(select(Application).where(Application.job_id == job.id).with_for_update()
                     .execution_options(populate_existing=True).limit(1))
