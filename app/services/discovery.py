"""What happens to a job the moment it is discovered, whichever import found it.

    new job (Apify/Hunter sync, Lever/Greenhouse import)
      → domain filter (app.services.domain_match): not matching → hidden, nothing else happens
      → resume: the saved search's pinned resume, else the best-tagged resume (app.services.resume_matching), else
        none (the account's active resume, at use time)
      → description usable? parse it : recover it first (app.services.jd_recovery)

A job the person adds themselves (source "manual") is always kept: adding it is the decision. Postings deleted
earlier because they no longer existed are not imported again (app.services.job_removal.is_removed); the import
paths check that before creating anything.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Job, Resume, SavedSearch, SearchRun
from app.services import domain_match, jd_recovery, job_description, resume_matching


def setup_resume_choice(db: Session, search_run_id: int | None, owner: int) -> tuple[str, int | None]:
    """What the saved search behind this run says about the resume: ("auto", None) — match by tags, the default —
    ("active", None) — always the active resume — or ("fixed", id) for a resume it pinned (that still exists)."""
    run = db.get(SearchRun, search_run_id) if search_run_id else None
    setup = db.get(SavedSearch, run.saved_search_id) if run is not None and run.saved_search_id else None
    settings = (setup.settings or {}) if setup is not None else {}
    chosen = settings.get("resume_id")
    mode = settings.get("resume_mode") or ("fixed" if isinstance(chosen, int) else "auto")
    if mode == "fixed" and isinstance(chosen, int):
        resume = db.get(Resume, chosen)
        return ("fixed", resume.id) if resume is not None and resume.user_id == owner else ("auto", None)
    return ("active", None) if mode == "active" else ("auto", None)


def choose_resume(db: Session, job: Job, search_run_id: int | None = None) -> None:
    """The saved search's pinned resume, else the best-tagged resume for the job (app.services.resume_matching),
    else none (the active resume). Does not commit."""
    if job.resume_choice is not None or job.resume_id is not None:
        return
    mode, resume_id = setup_resume_choice(db, search_run_id or job.search_run_id, job.user_id)
    if mode == "fixed":
        job.resume_id, job.resume_choice = resume_id, resume_matching.SETUP
    elif mode == "active":
        job.resume_choice = resume_matching.SETUP  # pinned to the active resume: never re-matched
    else:
        resume_matching.auto_assign(db, job)


def admit(db: Session, job: Job, *, search_run_id: int | None = None,
          rules_cache: dict[int, domain_match.Rules] | None = None) -> str:
    """Decide what a just-created (flushed) job needs. Returns created or not_matching. Does not commit."""
    if job.source == "manual":
        job.domain_match, job.domain_reason = domain_match.KEPT, "Added by you"
    elif not domain_match.screen(db, job, rules_cache, actor="discovery"):
        return "not_matching"
    choose_resume(db, job, search_run_id)
    if not jd_recovery.on_new_job(db, job):
        job_description.queue_parse(db, job)
    return "created"
