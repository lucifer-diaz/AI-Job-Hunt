from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent, CandidateProfile, Evidence, FollowUp, Job, ResumeVariant
from app.prompts import build_followup_prompt, build_ranking_prompt, build_review_prompt, build_tailoring_prompt
from app.services.contacts import recruiters_of
from app.services.llm import LLMClient
from app.services.resume_format import (ParsedResume, parse_resume, relevant_skills, render_legacy, render_resume,
                                        skills_text, summary_text, to_plain)
from app.services.user_settings import profile_for, settings_for
from app.settings import get_settings


def followup_days(db: Session) -> int:
    prefs = settings_for(db).preferences or {}
    return int(prefs.get("followup_days") or get_settings().followup_days)


def get_profile(db: Session) -> CandidateProfile:
    """The candidate profile of the user this session belongs to (app.tenancy)."""
    return profile_for(db)


def _evidence_payload(db: Session) -> list[dict]:
    items = db.scalars(select(Evidence).where(Evidence.verified.is_(True)).order_by(desc(Evidence.id))).all()
    return [
        {"id": e.id, "company": e.company, "role": e.role, "claim": e.claim,
         "context": e.context, "skills": e.skills, "tags": e.tags}
        for e in items
    ]


def rank_job(db: Session, job: Job) -> dict:
    profile = get_profile(db)
    evidence = _evidence_payload(db)
    prompt = build_ranking_prompt(candidate_profile=profile, resume=profile.master_resume, evidence=evidence, job=job)
    result = LLMClient.for_user(db).generate_prompt_json(prompt)
    job.fit_score = float(result.get("score", 0))
    job.fit_analysis = {**result, "prompt_version": prompt.version}
    # Re-ranking must never pull a job back out of tailoring, approval or the tracker.
    if job.status in ("discovered", "shortlisted", "archived"):
        job.status = "shortlisted" if job.fit_score >= get_settings().min_fit_score else "archived"
    db.commit()
    db.refresh(job)
    return result


# Called with the stage a tailoring run is entering ("ranking", "tailoring", "reviewing") so background tasks can
# report progress.
Progress = Callable[[str], None]


def _no_progress(_stage: str) -> None:
    return None


def tailor_job(db: Session, job: Job, progress: Progress = _no_progress) -> ResumeVariant:
    profile = get_profile(db)
    evidence = _evidence_payload(db)
    if job.fit_score is None:
        progress("ranking")
        rank_job(db, job)
    progress("tailoring")
    llm = LLMClient.for_user(db)
    parsed = parse_resume(profile.master_resume or "")
    roles_payload = [{"role_index": i, "title": r.title, "company": r.company, "dates": r.dates, "bullets": r.bullets}
                     for i, r in enumerate(parsed.roles)]
    prompt = build_tailoring_prompt(candidate_profile=profile, resume=profile.master_resume, roles=roles_payload,
                                    evidence=evidence, job=job, fit_analysis=job.fit_analysis, fit_score=job.fit_score)
    content = {**llm.generate_prompt_json(prompt), "prompt_version": prompt.version}
    markdown = build_resume_markdown(profile, content)
    version = (db.scalar(select(ResumeVariant.version).where(ResumeVariant.job_id == job.id).order_by(desc(ResumeVariant.version)).limit(1)) or 0) + 1
    resume = ResumeVariant(
        job_id=job.id,
        version=version,
        content=content,
        markdown=markdown,
        cover_letter=content.get("cover_letter", ""),
    )
    db.add(resume)
    job.status = "tailored"
    db.commit()
    db.refresh(resume)
    progress("reviewing")
    review_resume(db, resume)
    return resume


# Jobs that are in the Tailor queue and have not been approved or submitted yet.
SKIP_TAILORING_STATUSES = ("shortlisted", "tailored", "pending_approval")


def skip_tailoring(db: Session, job: Job, progress: Progress = _no_progress) -> ResumeVariant:
    """Use the master resume unchanged for a job that needs no tailoring. No AI edits; the reviewer still scores it."""
    profile = get_profile(db)
    if not (profile.master_resume or "").strip():
        raise ValueError("Add a master resume in Settings before skipping tailoring")
    markdown = render_resume(parse_resume(profile.master_resume), name=profile.name, headline=profile.headline)
    version = (db.scalar(select(ResumeVariant.version).where(ResumeVariant.job_id == job.id).order_by(desc(ResumeVariant.version)).limit(1)) or 0) + 1
    resume = ResumeVariant(
        job_id=job.id,
        version=version,
        content={"untailored": True, "summary": "", "skills": [], "experience": [], "selected_evidence_ids": [],
                 "cover_letter": ""},
        markdown=markdown,
        cover_letter="",
    )
    db.add(resume)
    job.status = "tailored"
    db.commit()
    db.refresh(resume)
    progress("reviewing")
    review_resume(db, resume)
    return resume


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]{3,}", (text or "").lower())}


def _best_match(text: str, candidates: list[str]) -> tuple[str, float]:
    target = _tokens(text)
    best, best_score = "", 0.0
    for candidate in candidates:
        words = _tokens(candidate)
        if not target or not words:
            continue
        score = len(target & words) / len(target)
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


NEW_BULLET = "New bullet - not a rewrite of an existing line"


def _role_bullets(parsed: ParsedResume, content: dict) -> dict[int, list[str]]:
    """Validated per-role bullets from the tailoring output; unknown roles are ignored."""
    out: dict[int, list[str]] = {}
    for item in content.get("experience") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("role_index")
        bullets = [str(b).strip() for b in item.get("bullets") or [] if str(b).strip()]
        if isinstance(index, int) and 0 <= index < len(parsed.roles) and bullets:
            out.setdefault(index, []).extend(bullets)
    return out


def edit_slots(profile: CandidateProfile, content: dict) -> tuple[ParsedResume, list[dict]]:
    """The reviewable units of a tailored resume. Edits and the rebuilt Markdown share this order."""
    parsed = parse_resume(profile.master_resume or "")
    primary = (profile.search_preferences or {}).get("primary_skills") or []
    slots = [
        {"section": "SUMMARY", "before": summary_text(parsed) or profile.headline or "", "after": content.get("summary", "")},
        {"section": "SKILLS", "before": skills_text(parsed) or ", ".join(primary) or "No skills listed on your profile",
         "after": ", ".join(str(s) for s in content.get("skills") or [])},
    ]
    roles = _role_bullets(parsed, content) if parsed.structured else {}
    if roles:
        for role_index, bullets in sorted(roles.items()):
            role = parsed.roles[role_index]
            for bullet in bullets:
                before, score = _best_match(bullet, role.bullets)
                slots.append({"section": " · ".join(filter(None, ["EXPERIENCE", role.title, role.company])),
                              "before": before if score >= 0.2 else NEW_BULLET, "after": bullet,
                              "role_index": role_index, "restore": before if score >= 0.2 else ""})
    else:
        master_lines = [line.strip(" -\t") for line in to_plain(profile.master_resume or "").splitlines() if len(line.strip()) > 20]
        section = "HIGHLIGHTS" if parsed.structured else "EXPERIENCE"
        for bullet in content.get("experience_bullets") or []:
            before, score = _best_match(bullet, master_lines)
            slots.append({"section": section, "before": before if score >= 0.2 else NEW_BULLET, "after": str(bullet),
                          "role_index": None, "restore": ""})
    for index, slot in enumerate(slots):
        slot["index"] = index
    return parsed, slots


def resume_edits(db: Session, resume: ResumeVariant) -> list[dict]:
    """Present a tailored resume as reviewable edits: before (master resume) → after (tailored)."""
    content = resume.content or {}
    if content.get("untailored"):
        return []  # the master resume is used as-is: nothing was rewritten
    profile = get_profile(db)
    state = content.get("edit_state") or {}
    evidence = db.scalars(select(Evidence).where(Evidence.verified.is_(True))).all()
    selected = set(content.get("selected_evidence_ids") or [])
    claims = {e.id: e for e in evidence}

    def cite(text: str) -> str:
        pool = [claims[i] for i in selected if i in claims] or list(claims.values())
        match, score = _best_match(text, [f"{e.claim} {' '.join(e.skills or [])}" for e in pool])
        if score < 0.15:
            return ""
        item = next(e for e in pool if f"{e.claim} {' '.join(e.skills or [])}" == match)
        return f"{item.company or 'verified'} · {item.claim[:80]}"

    edits = []
    for slot in edit_slots(profile, content)[1]:
        saved = state.get(str(slot["index"])) or {}
        text = saved.get("text") or slot["after"]
        if slot["section"] == "SKILLS":
            evidence_label = "master resume skills"
        else:
            evidence_label = cite(text) or (f"master resume · {slot['restore'][:70]}" if slot.get("restore") else "")
        edits.append({"index": slot["index"], "section": slot["section"], "before": slot["before"], "after": text,
                      "evidence": evidence_label, "status": saved.get("status") or ("accepted" if evidence_label else "review"),
                      "edited": bool(saved.get("text"))})
    return edits


def build_resume_markdown(profile: CandidateProfile, content: dict) -> str:
    """Rebuild the full resume: master structure, tailored wording, never the raw source text."""
    parsed, slots = edit_slots(profile, content)
    state = content.get("edit_state") or {}

    def resolved(slot: dict) -> str | None:
        saved = state.get(str(slot["index"])) or {}
        if saved.get("status") == "reverted":
            return None
        return saved.get("text") or slot["after"]

    summary, skills_value = resolved(slots[0]), resolved(slots[1])
    skill_list = [s.strip() for s in (skills_value or "").split(",") if s.strip()]
    if not parsed.structured:
        bullets = [value for value in (resolved(s) for s in slots[2:]) if value]
        return render_legacy(profile.name, profile.headline, summary or profile.headline or "", skill_list, bullets)
    role_bullets: dict[int, list[str]] = {}
    highlights: list[str] = []
    for slot in slots[2:]:
        value = resolved(slot)
        if slot["role_index"] is None:
            if value:
                highlights.append(value)
            continue
        value = value if value is not None else slot["restore"]  # a reverted rewrite restores its master bullet
        bucket = role_bullets.setdefault(slot["role_index"], [])
        if value and value not in bucket:
            bucket.append(value)
    return render_resume(parsed, name=profile.name, headline=profile.headline, summary=summary or None,
                         top_skills=relevant_skills(parsed, skill_list), role_bullets=role_bullets, highlights=highlights)


def update_resume_edit(db: Session, resume: ResumeVariant, index: int, status: str | None, text: str | None) -> None:
    content = dict(resume.content or {})
    state = dict(content.get("edit_state") or {})
    entry = dict(state.get(str(index)) or {})
    if status:
        entry["status"] = status
    if text is not None:
        entry["text"] = text.strip()
        entry.setdefault("status", "accepted")
    state[str(index)] = entry
    content["edit_state"] = state
    resume.content = content
    resume.markdown = build_resume_markdown(get_profile(db), content)
    db.commit()


def review_resume(db: Session, resume: ResumeVariant) -> dict:
    llm = LLMClient.for_user(db)
    job = db.get(Job, resume.job_id)
    prompt = build_review_prompt(candidate_profile=get_profile(db), evidence=_evidence_payload(db), job=job,
                                 tailored_resume=resume.markdown)
    result = {**llm.generate_prompt_json(prompt), "prompt_version": prompt.version}
    resume.reviewer_score = float(result.get("score", 0))
    resume.reviewer_notes = result
    db.commit()
    return result


def approve_job(db: Session, job: Job) -> Application:
    latest = db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job.id)
                       .order_by(desc(ResumeVariant.version)).limit(1))
    if not latest or latest.reviewer_score is None:
        raise ValueError("The latest resume must be reviewed before approval")
    application = job.application
    if not application:
        application = Application(job_id=job.id)
        db.add(application)
    application.status = "approved"
    application.approved_at = datetime.now(UTC).replace(tzinfo=None)
    application.approved_resume_id = latest.id
    job.status = "approved"
    db.commit()
    db.refresh(application)
    return application


def mark_applied(db: Session, job: Job) -> Application:
    application = job.application or approve_job(db, job)
    if application.status != "approved":
        raise ValueError("Application must be approved before marking applied")
    applied_at = datetime.now(UTC).replace(tzinfo=None)
    application.status = "applied"
    application.stage = "applied"
    application.applied_at = applied_at
    application.followup_due_at = applied_at + timedelta(days=followup_days(db))
    job.status = "applied"
    db.add(ApplicationEvent(application=application, kind="applied", title="Applied",
                            detail=f"Submitted by you · {job.url or 'no posting URL'}", occurred_at=applied_at))
    db.commit()
    db.refresh(application)
    return application


def create_due_followups(db: Session) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    apps = db.scalars(
        select(Application).where(
            Application.status == "applied",
            Application.followup_due_at.is_not(None),
            Application.followup_due_at <= now,
        )
    ).all()
    created = 0
    for app in apps:
        existing = db.scalar(select(FollowUp).where(FollowUp.application_id == app.id))
        if existing:
            continue
        draft_followup(db, app, commit=False)
        created += 1
    db.commit()
    return created


def draft_followup(db: Session, app: Application, commit: bool = True) -> FollowUp:
    """Generate (or regenerate) the follow-up draft for one application. Never sends anything."""
    now = datetime.now(UTC).replace(tzinfo=None)
    llm = LLMClient.for_user(db)
    profile = get_profile(db)
    job = db.get(Job, app.job_id)
    sent = db.scalars(select(FollowUp.draft).where(FollowUp.application_id == app.id, FollowUp.status != "draft")
                      .order_by(FollowUp.id)).all()
    prompt = build_followup_prompt(
        candidate_profile=profile, resume=profile.master_resume, evidence=_evidence_payload(db), job=job,
        application={"status": app.status, "applied_at": app.applied_at.isoformat() if app.applied_at else ""},
        recruiters=recruiters_of(app), previous_followups=[text for text in sent if text])
    result = llm.generate_prompt_json(prompt)
    fu = db.scalar(select(FollowUp).where(FollowUp.application_id == app.id, FollowUp.status == "draft")
                   .order_by(desc(FollowUp.id)).limit(1))
    if not fu:
        fu = FollowUp(application_id=app.id, due_at=app.followup_due_at or now, channel="email", status="draft")
        db.add(fu)
    fu.draft = result.get("draft", "")
    if commit:
        db.commit()
        db.refresh(fu)
    return fu
