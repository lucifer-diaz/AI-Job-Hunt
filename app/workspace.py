"""API for the redesigned workspace: overview, today, triage, tailor, approvals, tracker.

Every number the UI shows is derived from the database here; nothing is hard-coded.
Job Hunt OS never submits applications — the apply kit hands the final click to the user.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.scheduler_hooks import run_soon
from app.models import (AppSetting, Application, ApplicationEvent, Evidence, FollowUp, Job, KnowledgeAnswer,
                        ResumeDocument, ResumeVariant, SearchRun)
from app.services.actors import (BUILTIN, DEFAULT_ACTORS, FAN_OUTS, MAX_RUNS, PLACEHOLDERS, REMOTE_PLATFORMS,
                                 SENIORITY_LEVELS, catalog as actor_catalog, describe_input, plan_runs)
from app.services.contacts import (MAX_RECRUITERS, ContactIn, clean_email, clean_phone, event_meta, format_in_zone,
                                   recruiters_of, set_primary_recruiter, set_recruiters, to_utc, zone)
from app.services.ai_config import STAGES as AI_STAGES, is_free_model, resolve as resolve_ai_config
from app.prompts import build_evidence_extraction_prompt
from app.services.llm import LLMCall, LLMClient
from app.services.sources import (UA, import_remote_run, list_remote_runs, refresh_apify_run, start_apify_search,
                                  upsert_job)
from app.services.user_settings import apify_token, lock_settings_row, settings_for
from app.services import ranker
from app.services.workflow import draft_followup, followup_days, get_profile, resume_edits, update_resume_edit
from app.settings import get_settings

router = APIRouter()

BASE_STAGES = [
    {"id": "applied", "label": "Applied", "dot": "#5b4ae8"},
    {"id": "acknowledged", "label": "Acknowledged", "dot": "#a79bf0"},
    {"id": "screening", "label": "Screening", "dot": "#0f9c86"},
    {"id": "interviewing", "label": "Interviewing", "dot": "#0e7a68"},
]
EXTRA_STAGES = [
    {"id": "takehome", "label": "Take-home", "dot": "#e0a136"},
    {"id": "offer", "label": "Offer", "dot": "#0f9c86"},
    {"id": "negotiating", "label": "Negotiating", "dot": "#b26b00"},
    {"id": "onhold", "label": "On hold", "dot": "#9aa1b2"},
    {"id": "rejected", "label": "Rejected", "dot": "#c22f4e"},
    {"id": "referral", "label": "Referral outreach", "dot": "#a79bf0"},
]
CLOSED_STAGE = {"id": "closed", "label": "Closed", "dot": "#c6cad8"}
ALL_STAGE_IDS = {s["id"] for s in BASE_STAGES + EXTRA_STAGES} | {"closed"}
REPLY_STAGES = {"screening", "interviewing", "takehome", "offer", "negotiating"}
INTERVIEW_STAGES = {"interviewing", "takehome", "offer", "negotiating"}
INACTIVE_STAGES = {"closed", "rejected"}
TRIAGE_ACTIONS = {"shortlist": "shortlisted", "maybe": "maybe", "skip": "dismissed", "dismiss": "dismissed",
                  "restore": "discovered"}
QUEUE_STATUSES = ("discovered", "shortlisted", "archived")
AGENCY_HINTS = ("staffing", "recruit", "consultan", "talent", "placement", "manpower", "hr services", "headhunt")
BOARDS = ["linkedin", "indeed", "glassdoor", "naukri", "bayt", "zip_recruiter"]
DEFAULT_PREFS = {"weekly_target": 10, "followup_days": None, "email_run_sheet": False, "warn_closing": True,
                 "extra_stages": []}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def setting_row(db: Session) -> AppSetting:
    return settings_for(db)


def prefs(db: Session) -> dict:
    merged = {**DEFAULT_PREFS, **(setting_row(db).preferences or {})}
    merged["followup_days"] = followup_days(db)
    return merged


def board_label(source: str) -> str:
    key = (source or "").split(":", 1)[-1]
    return {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor", "naukri": "Naukri",
            "bayt": "Bayt", "zip_recruiter": "ZipRecruiter", "manual": "Manual", "lever": "Lever",
            "greenhouse": "Greenhouse", "unknown": "Other"}.get(key, key.replace("_", " ").title() or "Other")


def is_agency(company: str) -> bool:
    lowered = (company or "").lower()
    return any(hint in lowered for hint in AGENCY_HINTS)


def ago(value: datetime | None) -> str:
    if not value:
        return "—"
    seconds = max(0, (utcnow() - value).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def day_label(value: datetime | None) -> str:
    return value.strftime("%-d %b") if value else ""


def search_prefs(db: Session) -> dict:
    return dict(get_profile(db).search_preferences or {})


def excluded_companies(db: Session) -> set[str]:
    return {c.strip().lower() for c in search_prefs(db).get("exclude_companies") or [] if c.strip()}


def latest_resume(db: Session, job_id: int) -> ResumeVariant | None:
    return db.scalar(select(ResumeVariant).where(ResumeVariant.job_id == job_id)
                     .order_by(desc(ResumeVariant.version)).limit(1))


def job_card(job: Job) -> dict:
    analysis = job.fit_analysis or {}
    app = job.application
    return {
        "id": job.id, "title": job.title, "company": job.company, "location": job.location, "url": job.url,
        "source": job.source, "board": board_label(job.source),
        "fit": None if job.fit_score is None else round(job.fit_score),
        "status": job.status, "discovered_at": job.discovered_at.isoformat(), "posted": ago(job.discovered_at),
        "matches": analysis.get("strong_matches") or [], "gaps": analysis.get("gaps") or [],
        "missing": analysis.get("must_have_missing") or [], "keywords": analysis.get("keywords") or [],
        "summary": analysis.get("summary") or "", "strategy": analysis.get("application_strategy") or "",
        "snippet": (job.description or "")[:360], "agency": is_agency(job.company),
        "search_run_id": job.search_run_id, "application_id": app.id if app else None,
        "application_status": app.status if app else None, "stage": app.stage if app else None,
    }


def get_job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ───────────────────────────── summary / overview / today ─────────────────────────────

def triage_query(db: Session):
    query = select(Job).where(Job.triaged_at.is_(None), Job.status.in_(QUEUE_STATUSES))
    excluded = excluded_companies(db)
    if excluded:
        query = query.where(func.lower(Job.company).not_in(excluded))
    return query


def approval_jobs(db: Session) -> list[Job]:
    return db.scalars(select(Job).where(Job.status.in_(("tailored", "pending_approval")))
                      .order_by(desc(Job.fit_score))).all()


def live_apps(db: Session) -> list[Application]:
    return db.scalars(select(Application).where(Application.applied_at.is_not(None))).all()


@router.get("/api/workspace/summary")
def summary(db: Session = Depends(get_user_db)):
    row = setting_row(db)
    profile = get_profile(db)
    sp = search_prefs(db).get("search_profile") or {}
    apps = live_apps(db)
    live = [a for a in apps if (a.stage or "applied") not in INACTIVE_STAGES]
    tailor_count = db.scalar(select(func.count(Job.id)).where(Job.status == "shortlisted", Job.triaged_at.is_not(None),
                                                              ~Job.resumes.any())) or 0
    last_run = db.scalar(select(SearchRun).order_by(desc(SearchRun.id)).limit(1))
    return {
        "counts": {"today": len(today_actions(db)), "discovery": db.scalar(select(func.count()).select_from(triage_query(db).subquery())) or 0,
                   "tailor": tailor_count, "approvals": len(approval_jobs(db)), "tracker": len(live)},
        "profile": {"name": profile.name, "headline": profile.headline},
        "llm_provider": row.llm_provider, "model": row.model, "application_mode": row.application_mode,
        "auto_search": {"schedule": sp.get("schedule", "manual"), "roles": len(sp.get("roles") or []),
                        "locations": len(sp.get("locations") or []),
                        "last_run": last_run.created_at.isoformat() if last_run else None,
                        "last_run_status": last_run.status if last_run else None},
        "total_jobs": db.scalar(select(func.count(Job.id))) or 0,
    }


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "0%"


@router.get("/api/workspace/overview")
def overview(db: Session = Depends(get_user_db)):
    since = utcnow() - timedelta(days=30)
    jobs = db.scalars(select(Job).where(Job.discovered_at >= since)).all()
    total = len(jobs)
    shortlisted = [j for j in jobs if j.status not in ("discovered", "archived", "dismissed", "maybe")]
    tailored = [j for j in jobs if j.resumes]
    approved = [j for j in jobs if j.application and j.application.approved_at]
    applied = [j for j in jobs if j.application and j.application.applied_at]
    interviewing = [j for j in applied if (j.application.stage or "") in INTERVIEW_STAGES]
    funnel = [
        ("Discovered", len(jobs), "", "#c6cad8", "discovery"),
        ("Shortlisted", len(shortlisted), f"{_pct(len(shortlisted), total)} kept", "#a79bf0", "discovery"),
        ("Tailored", len(tailored), f"{_pct(len(tailored), len(shortlisted))} of shortlist", "#7c69ee", "tailor"),
        ("Approved", len(approved), f"{_pct(len(approved), len(tailored))} pass review", "#5b4ae8", "approvals"),
        ("Applied", len(applied), f"{_pct(len(applied), len(approved))} submitted", "#0f9c86", "tracker"),
        ("Interviewing", len(interviewing), f"{_pct(len(interviewing), len(applied))} of applied", "#0e7a68", "tracker"),
    ]

    by_board: dict[str, dict] = defaultdict(lambda: {"found": 0, "applied": 0, "replies": 0})
    for job in jobs:
        row = by_board[board_label(job.source)]
        row["found"] += 1
        if job.application and job.application.applied_at:
            row["applied"] += 1
            if (job.application.stage or "") in REPLY_STAGES:
                row["replies"] += 1
    source_perf = sorted(({"board": k, **v, "rate": _pct(v["replies"], v["applied"]) if v["applied"] else "—"}
                          for k, v in by_board.items()), key=lambda r: -r["found"])

    now = utcnow()
    upcoming = []
    for app in live_apps(db):
        if (app.stage or "applied") in INACTIVE_STAGES:
            continue
        if app.followup_due_at and app.followup_due_at <= now + timedelta(days=10) and app.stage == "applied":
            upcoming.append({"date": app.followup_due_at.isoformat(), "label": f"Follow-up · {app.job.company}",
                             "kind": "nudge", "job_id": app.job_id})
        for event in app.events:
            if event.kind == "interview" and now <= event.occurred_at <= now + timedelta(days=10):
                upcoming.append({"date": event.occurred_at.isoformat(), "label": f"{event.title} · {app.job.company}",
                                 "kind": "interview", "job_id": app.job_id})
    upcoming.sort(key=lambda u: u["date"])

    target = prefs(db)["weekly_target"]
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = []
    all_applied = [a.applied_at for a in live_apps(db)]
    for offset in range(5, -1, -1):
        start = week_start - timedelta(weeks=offset)
        end = start + timedelta(weeks=1)
        weeks.append({"label": f"W{start.isocalendar()[1]}", "n": sum(1 for d in all_applied if start <= d < end)})
    peak = max([target, *[w["n"] for w in weeks]]) or 1
    for w in weeks:
        w["pct"] = f"{100 * w['n'] / peak:.0f}%"

    roles = [r for r in get_profile(db).target_roles or [] if r.strip()]
    mix: Counter = Counter()
    for job in jobs:
        title = job.title.lower()
        match = next((r for r in roles if set(r.lower().split()) & set(title.split()) - {"engineer", "senior", "lead", "-", "—"}), None)
        mix[match or "Other roles"] += 1
    role_mix = [{"label": label, "n": n, "pct": _pct(n, total)} for label, n in mix.most_common(4)]
    fits = [j.fit_score for j in jobs if j.fit_score is not None]

    def avg_days(pairs):
        values = [(b - a).total_seconds() / 86400 for a, b in pairs if a and b and b >= a]
        return round(sum(values) / len(values), 1) if values else None

    triage_pairs = [(j.discovered_at, j.triaged_at) for j in jobs]
    apply_pairs = [(j.triaged_at or j.discovered_at, j.application.applied_at) for j in applied]
    reply_pairs = []
    for j in applied:
        reply = next((e.occurred_at for e in j.application.events
                      if e.kind == "stage" and e.detail in REPLY_STAGES), None)
        reply_pairs.append((j.application.applied_at, reply))
    stale = [j for j in approved if not j.application.applied_at and j.application.approved_at < now - timedelta(days=2)]
    return {
        "total": total, "funnel": [{"label": l, "n": n, "conv": c, "color": col, "screen": s,
                                    "pct": _pct(n, total) if total else "0%"} for l, n, c, col, s in funnel],
        "source_perf": source_perf, "upcoming": upcoming[:6], "weeks": weeks, "weekly_target": target,
        "role_mix": role_mix, "median_fit": round(median(fits)) if fits else None,
        "time_in_stage": {"triage": avg_days(triage_pairs), "apply": avg_days(apply_pairs), "reply": avg_days(reply_pairs)},
        "stale_approved": len(stale), "approvals": len(approval_jobs(db)),
    }


def today_actions(db: Session) -> list[dict]:
    now = utcnow()
    actions = []
    for job in approval_jobs(db):
        resume = latest_resume(db, job.id)
        if resume and resume.reviewer_score is not None:
            gaps = len((job.fit_analysis or {}).get("gaps") or [])
            actions.append({"kind": "approve", "job_id": job.id, "company": job.company, "title": job.title,
                            "fit": job.fit_score, "priority": 0, "minutes": 6,
                            "reason": f"Tailored packet scored {resume.reviewer_score:.0f} with the reviewer."
                                      + (f" {gaps} gap flag{'s' if gaps != 1 else ''} to check." if gaps else "")})
    for job in db.scalars(select(Job).where(Job.status == "shortlisted", Job.triaged_at.is_not(None), ~Job.resumes.any())
                          .order_by(desc(Job.fit_score)).limit(5)).all():
        matches = len((job.fit_analysis or {}).get("strong_matches") or [])
        actions.append({"kind": "tailor", "job_id": job.id, "company": job.company, "title": job.title,
                        "fit": job.fit_score, "priority": 1, "minutes": 12,
                        "reason": f"Shortlisted {ago(job.triaged_at or job.discovered_at)}."
                                  + (f" {matches} strong matches in your evidence." if matches else "")})
    for app in live_apps(db):
        if app.stage == "applied" and app.followup_due_at and app.followup_due_at <= now:
            days = (now - app.applied_at).days
            actions.append({"kind": "follow_up", "job_id": app.job_id, "application_id": app.id,
                            "company": app.job.company, "title": app.job.title, "fit": app.job.fit_score,
                            "priority": 2, "minutes": 5,
                            "reason": f"Applied {days} day{'s' if days != 1 else ''} ago with no reply logged."})
    actions.sort(key=lambda a: (a["priority"], -(a["fit"] or 0)))
    return actions


@router.get("/api/workspace/today")
def today(db: Session = Depends(get_user_db)):
    now = utcnow()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    apps = live_apps(db)
    this_week = [a for a in apps if a.applied_at >= week_start]
    stage_events = db.scalars(select(ApplicationEvent).where(ApplicationEvent.kind == "stage",
                                                             ApplicationEvent.occurred_at >= week_start)).all()
    replies_week = len({e.application_id for e in stage_events if e.detail in REPLY_STAGES})
    interviews_week = len({e.application_id for e in stage_events if e.detail in INTERVIEW_STAGES})
    replied = [a for a in apps if (a.stage or "") in REPLY_STAGES]
    followups = []
    for app in sorted(apps, key=lambda a: a.followup_due_at or now):
        if app.stage != "applied" or not app.followup_due_at or app.followup_due_at > now + timedelta(days=3):
            continue
        delta = (app.followup_due_at.date() - now.date()).days
        due = f"Overdue {-delta}d" if delta < 0 else "Due today" if delta == 0 else f"In {delta} days"
        followups.append({"application_id": app.id, "job_id": app.job_id, "company": app.job.company,
                          "role": app.job.title, "due": due, "tone": "overdue" if delta < 0 else "today" if delta == 0 else "soon",
                          "detail": f"Applied {day_label(app.applied_at)} · "
                                    + (f"recruiter {app.recruiter_name}" if app.recruiter_name else "no contact yet")})
    days = []
    for i in range(7):
        start = week_start + timedelta(days=i)
        days.append({"label": "MTWTFSS"[i], "n": sum(1 for a in this_week if start <= a.applied_at < start + timedelta(days=1)),
                     "future": start.date() > now.date()})
    pending_triage = db.scalar(select(func.count()).select_from(triage_query(db).subquery())) or 0
    triaged_today = db.scalar(select(func.count(Job.id)).where(Job.triaged_at >= now.replace(hour=0, minute=0))) or 0
    unverified = db.scalar(select(func.count(KnowledgeAnswer.id)).where(KnowledgeAnswer.verified.is_(False))) or 0
    target = prefs(db)["weekly_target"]
    overview_data = overview(db)
    return {
        "date": now.isoformat(), "actions": today_actions(db)[:5], "followups": followups[:5],
        "week": {"applied": len(this_week), "target": target, "replies": replies_week, "interviews": interviews_week,
                 "response_rate": _pct(len(replied), len(apps)) if apps else "—", "days": days},
        "funnel": overview_data["funnel"], "pending_triage": pending_triage, "triaged_today": triaged_today,
        "approvals": len(approval_jobs(db)), "unverified_answers": unverified,
    }


# ───────────────────────────── discovery ─────────────────────────────

def _dedupe_key(job: Job) -> str:
    return re.sub(r"[^a-z0-9]+", " ", f"{job.company}|{job.title}".lower()).strip()


@router.get("/api/jobs/search")
def jobs_search(q: str = "", min_fit: int = 0, board: str = "all", stage: str = "all", sort: str = "fit",
                page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=200), queue: bool = False,
                hide_agencies: bool = False, hide_reposts: bool = False, keywords: str = "",
                ids_only: bool = False, db: Session = Depends(get_user_db)):
    query = triage_query(db) if queue else select(Job)
    jobs = db.scalars(query.order_by(Job.id)).all()
    excluded = excluded_companies(db)
    total_all = len(jobs)
    needle = q.lower().strip()
    words = [w.strip().lower() for w in keywords.split(",") if w.strip()]
    seen: set[str] = set()
    rows = []
    for job in jobs:
        key = _dedupe_key(job)
        repost = key in seen
        seen.add(key)
        if job.company.lower() in excluded:
            continue
        if min_fit and (job.fit_score or 0) < min_fit:
            continue
        if board != "all" and board_label(job.source) != board:
            continue
        if stage == "unranked":  # not a status: any job without a fit score yet
            if job.fit_score is not None:
                continue
        elif stage != "all" and job.status != stage:
            continue
        haystack = f"{job.title} {job.company} {job.location}".lower()
        if needle and needle not in haystack:
            continue
        if words and not all(any(part in f"{haystack} {job.description.lower()[:4000]}" for part in w.split("/")) for w in words):
            continue
        if hide_agencies and is_agency(job.company):
            continue
        if hide_reposts and repost:
            continue
        rows.append(job)
    if sort == "new":
        rows.sort(key=lambda j: j.discovered_at, reverse=True)
    elif sort == "company":
        rows.sort(key=lambda j: j.company.lower())
    else:
        rows.sort(key=lambda j: (j.fit_score is None, -(j.fit_score or 0), -j.id))
    total = len(rows)
    if ids_only:  # "select all matching" across every page
        return {"ids": [j.id for j in rows[:5000]], "total": total}
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    boards = sorted({board_label(j.source) for j in jobs})
    return {"items": [job_card(j) for j in rows[(page - 1) * per_page: page * per_page]], "total": total,
            "total_all": total_all, "page": page, "pages": pages, "per_page": per_page, "boards": boards}


class TriageIn(BaseModel):
    action: str


class BulkTriageIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: str


def apply_triage(job: Job, action: str) -> None:
    if action not in TRIAGE_ACTIONS:
        raise HTTPException(422, "Unknown triage action")
    if job.status in ("tailored", "approved", "applied", "pending_approval") and action != "shortlist":
        raise HTTPException(409, "This job already has a tailored packet or application")
    if job.status not in ("tailored", "approved", "applied", "pending_approval"):
        job.status = TRIAGE_ACTIONS[action]
    job.triaged_at = None if action == "restore" else utcnow()


@router.post("/api/jobs/{job_id}/triage")
def triage(job_id: int, payload: TriageIn, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    apply_triage(job, payload.action)
    db.commit()
    return job_card(job)


@router.post("/api/jobs/bulk-triage")
def bulk_triage(payload: BulkTriageIn, db: Session = Depends(get_user_db)):
    changed = 0
    for job in db.scalars(select(Job).where(Job.id.in_(payload.ids))).all():
        try:
            apply_triage(job, payload.action)
            changed += 1
        except HTTPException:
            continue
    db.commit()
    return {"changed": changed}


class BulkIdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    include_submitted: bool = False


PROTECTED_STATUSES = ("approved", "applied")


@router.post("/api/jobs/bulk-delete")
def bulk_delete(payload: BulkIdsIn, db: Session = Depends(get_user_db)):
    """Permanently delete jobs with their tailored packets. Approved or applied jobs need include_submitted."""
    deleted, protected = 0, []
    for job in db.scalars(select(Job).where(Job.id.in_(payload.ids))).all():
        if job.status in PROTECTED_STATUSES and not payload.include_submitted:
            protected.append(job.id)
            continue
        db.delete(job)
        deleted += 1
    db.commit()
    return {"deleted": deleted, "protected": protected,
            "missing": max(0, len(set(payload.ids)) - deleted - len(protected))}


@router.post("/api/jobs/bulk-rank", status_code=202)
def bulk_rank(payload: BulkIdsIn, request: Request, background: BackgroundTasks, rerank: bool = False,
              db: Session = Depends(get_user_db)):
    """Queue ranking for selected jobs. `rerank` also re-ranks ranked jobs as a new version."""
    out = ranker.request_ranking(db, payload.ids, force=rerank, only_unranked=not rerank, actor=f"user:{db.info['user_id']}")
    run_soon(background, request.app, "ranking")
    return out


@router.get("/api/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    resume = latest_resume(db, job.id)
    app = job.application
    fits = sorted((j for j in db.scalars(select(Job.fit_score).where(Job.fit_score.is_not(None))).all()), reverse=True)
    rank = None
    if job.fit_score is not None and fits:
        rank = round(100 * (fits.index(job.fit_score) + 1) / len(fits))
    steps = [
        ("Discovered", True, ago(job.discovered_at)),
        ("Ranked", job.fit_score is not None, f"fit {job.fit_score:.0f}" if job.fit_score is not None else "not yet"),
        ("Tailored", resume is not None, f"v{resume.version} · reviewer {resume.reviewer_score:.0f}" if resume and resume.reviewer_score is not None else "not yet"),
        ("Approved", bool(app and app.approved_at), day_label(app.approved_at) if app and app.approved_at else "not yet"),
        ("Applied", bool(app and app.applied_at), day_label(app.applied_at) if app and app.applied_at else "your action"),
    ]
    evidence = db.scalars(select(Evidence).where(Evidence.verified.is_(True))).all()
    gap_notes = []
    for gap in [*((job.fit_analysis or {}).get("must_have_missing") or []), *((job.fit_analysis or {}).get("gaps") or [])][:4]:
        words = {w for w in re.findall(r"[a-z0-9]{3,}", str(gap).lower())}
        partial = next((e for e in evidence if words & set(re.findall(r"[a-z0-9]{3,}", f"{e.claim} {' '.join(e.skills or [])}".lower()))), None)
        gap_notes.append({"gap": str(gap), "partial": f"{partial.company or 'Evidence'}: {partial.claim}" if partial else ""})
    return {**job_card(job), "description": job.description, "fit_analysis": job.fit_analysis or {},
            "rank_pct": rank, "wizard": [{"label": l, "done": d, "meta": m} for l, d, m in steps],
            "gap_notes": gap_notes, "notes": app.notes if app else "",
            "resume_id": resume.id if resume else None}


class JobUrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=1200)
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""


def _safe_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(422, "Enter a full http(s) job URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise HTTPException(422, "That host could not be resolved") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise HTTPException(422, "Private network addresses are not allowed")


def extract_job_posting(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    found: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]):
                org = item.get("hiringOrganization") or {}
                place = item.get("jobLocation") or {}
                place = place[0] if isinstance(place, list) and place else place
                address = (place or {}).get("address") or {} if isinstance(place, dict) else {}
                found = {"title": item.get("title", ""), "company": org.get("name", "") if isinstance(org, dict) else str(org),
                         "location": ", ".join(filter(None, [address.get("addressLocality"), address.get("addressCountry") if isinstance(address.get("addressCountry"), str) else ""])) if isinstance(address, dict) else "",
                         "description": BeautifulSoup(item.get("description", ""), "html.parser").get_text("\n", strip=True)}
                break
    meta = lambda prop: (soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop}) or {}).get("content", "")
    found.setdefault("title", "")
    found["title"] = found["title"] or meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    found["company"] = found.get("company") or meta("og:site_name")
    if not found.get("description"):
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        found["description"] = soup.get_text("\n", strip=True)[:30000]
    return found


@router.post("/api/jobs/from-url")
def job_from_url(payload: JobUrlIn, db: Session = Depends(get_user_db)):
    data = {"title": payload.title, "company": payload.company, "location": payload.location,
            "description": payload.description}
    fetch_error = ""
    if not (data["title"] and data["company"] and data["description"]):
        _safe_public_url(payload.url)
        try:
            with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as client:
                resp = client.get(payload.url)
                resp.raise_for_status()
            extracted = extract_job_posting(resp.text)
            data = {k: data[k] or extracted.get(k, "") for k in data}
        except httpx.HTTPError as exc:
            fetch_error = f"Could not fetch the posting ({exc.__class__.__name__})"
    missing = [k for k in ("title", "company") if not data[k].strip()]
    if missing:
        raise HTTPException(422, {"message": fetch_error or "Could not read the posting — fill in the missing fields",
                                  "missing": missing, "extracted": data})
    ref = hashlib.sha256(payload.url.encode()).hexdigest()[:40]
    job, created = upsert_job(db, source="manual", source_ref=ref, title=data["title"], company=data["company"],
                              location=data["location"], url=payload.url, description=data["description"] or data["title"])
    return {"created": created, "job": job_card(job)}


class CustomActorIn(BaseModel):
    id: str = Field(pattern=r"^[\w.-]+[/~][\w.-]+$", max_length=200)
    name: str = Field(default="", max_length=120)
    fan_out: str = "role_location"
    input_template: dict = Field(default_factory=dict)


class SearchProfileIn(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=5)
    locations: list[str] = Field(default_factory=list, max_length=5)
    actors: list[str] = Field(default_factory=lambda: list(DEFAULT_ACTORS), max_length=10)
    custom_actors: list[CustomActorIn] = Field(default_factory=list, max_length=10)
    sites: list[str] = Field(default_factory=lambda: ["linkedin", "indeed", "glassdoor"])
    remote_platforms: list[str] = Field(default_factory=lambda: list(REMOTE_PLATFORMS))
    remoteok_url: str = "https://remoteok.com/remote-jobs"
    hours_old: int = Field(default=168, ge=0, le=8760)
    max_results: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1000)
    job_type: str = ""
    seniority: list[str] = Field(default_factory=list)
    country_indeed: str = Field(default="usa", max_length=40)
    distance: int = Field(default=50, ge=0, le=500)
    is_remote: bool = False
    easy_apply: bool = False
    fetch_description: bool = True
    enforce_annual_salary: bool = False
    residential_proxy: bool = True
    salary_only: bool = False
    visa_sponsorship: bool = False
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)
    linkedin_company_ids: list[str] = Field(default_factory=list, max_length=20)
    skip_agencies: bool = True
    auto_rank: bool = True
    schedule: str = "manual"


JOB_TYPES = {"", "fulltime", "parttime", "contract", "internship", "temporary"}


def get_search_profile(db: Session) -> dict:
    profile = get_profile(db)
    saved = dict((profile.search_preferences or {}).get("search_profile") or {})
    if not isinstance(saved.get("seniority"), list):  # older profiles stored "any"
        saved.pop("seniority", None)
    base = SearchProfileIn(roles=(profile.target_roles or [])[:5], locations=(profile.preferred_locations or [])[:5]).model_dump()
    return {**base, **saved}


def validate_search_profile(payload: SearchProfileIn) -> None:
    problems = []
    if payload.schedule not in ("manual", "daily"):
        problems.append("Schedule must be manual or daily")
    if payload.job_type not in JOB_TYPES:
        problems.append("Unknown job type")
    if set(payload.sites) - set(BOARDS):
        problems.append(f"Unknown boards: {', '.join(sorted(set(payload.sites) - set(BOARDS)))}")
    if set(payload.remote_platforms) - set(REMOTE_PLATFORMS):
        problems.append("Unknown remote job boards")
    if set(payload.seniority) - set(SENIORITY_LEVELS):
        problems.append("Unknown seniority level")
    for entry in payload.custom_actors:
        if entry.fan_out not in FAN_OUTS:
            problems.append(f"{entry.id}: unknown fan-out '{entry.fan_out}'")
        if entry.id in BUILTIN:
            problems.append(f"{entry.id} is already built in")
    known = set(BUILTIN) | {c.id for c in payload.custom_actors}
    if not payload.actors:
        problems.append("Choose at least one Apify actor")
    if set(payload.actors) - known:
        problems.append(f"Unknown actors: {', '.join(sorted(set(payload.actors) - known))}")
    if "openclawai/job-board-scraper" in payload.actors and not payload.sites:
        problems.append("Choose at least one board for Multi Job Board")
    if problems:
        raise HTTPException(422, "; ".join(problems))


@router.get("/api/search/actors")
def search_actors(db: Session = Depends(get_user_db)):
    sp = get_search_profile(db)
    return {"actors": [spec.public() for spec in actor_catalog(sp).values()], "placeholders": list(PLACEHOLDERS),
            "fan_outs": list(FAN_OUTS), "boards": BOARDS, "remote_platforms": REMOTE_PLATFORMS,
            "seniority": SENIORITY_LEVELS, "max_runs": MAX_RUNS}


class ActorLookupIn(BaseModel):
    id: str = Field(pattern=r"^[\w.-]+[/~][\w.-]+$", max_length=200)


GUESSES = [(re.compile(r"^(search_?term|keywords?|query|search|search_?keywords?|title|position)$", re.I), "{{role}}"),
           (re.compile(r"^(search_?terms|titles|queries|title_?search)$", re.I), "{{roles}}"),
           (re.compile(r"^(location|city|place|where)$", re.I), "{{location}}"),
           (re.compile(r"^(locations|location_?search)$", re.I), "{{locations}}"),
           (re.compile(r"^(max_?(items|results|jobs)|limit|results_?wanted|rows|count)$", re.I), "{{max_results}}"),
           (re.compile(r"^(hours_?old|posted_?within_?hours)$", re.I), "{{hours_old}}"),
           (re.compile(r"^(days_?old|posted_?within_?days|date_?posted)$", re.I), "{{days_old}}"),
           (re.compile(r"^(is_?remote|remote(_?only)?)$", re.I), "{{is_remote}}")]


@router.post("/api/search/actors/lookup")
def lookup_actor(payload: ActorLookupIn, db: Session = Depends(get_user_db)):
    """Confirm a Store actor exists and suggest an input template from its input schema."""
    token = apify_token(db)
    if not token:
        raise HTTPException(400, "Add your Apify API token in Settings to look actors up")
    api_id = payload.id.replace("/", "~")
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA}
    try:
        with httpx.Client(timeout=20, headers=headers) as client:
            actor = client.get(f"https://api.apify.com/v2/acts/{api_id}")
            if actor.status_code == 404:
                raise HTTPException(404, f"No Apify actor named '{payload.id}'")
            actor.raise_for_status()
            info = actor.json().get("data") or {}
            build = client.get(f"https://api.apify.com/v2/acts/{api_id}/builds/default")
            schema = {}
            if build.is_success:
                raw = (build.json().get("data") or {}).get("inputSchema") or "{}"
                schema = json.loads(raw) if isinstance(raw, str) else raw
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Could not reach Apify: {exc.__class__.__name__}") from exc
    properties = (schema or {}).get("properties") or {}
    template = {}
    for name, prop in properties.items():
        guess = next((value for pattern, value in GUESSES if pattern.match(name)), None)
        if guess:
            template[name] = guess
        elif name in (schema.get("required") or []) and "prefill" in prop:
            template[name] = prop["prefill"]
    return {"id": f"{info.get('username', payload.id.split('/')[0])}/{info.get('name', payload.id.split('/')[-1])}",
            "name": info.get("title") or info.get("name") or payload.id,
            "summary": (info.get("description") or "")[:300],
            "fields": [{"name": n, "type": p.get("type", ""), "title": p.get("title", ""), "required": n in (schema.get("required") or [])}
                       for n, p in properties.items()],
            "input_template": template}


@router.get("/api/search/profile")
def search_profile_get(db: Session = Depends(get_user_db)):
    since = utcnow() - timedelta(days=30)
    jobs = db.scalars(select(Job).where(Job.discovered_at >= since, Job.source.like("apify:%"))).all()
    stats = {}
    for site in BOARDS:
        mine = [j for j in jobs if j.source == f"apify:{site}"]
        kept = [j for j in mine if j.status not in ("discovered", "archived", "dismissed")]
        stats[site] = round(100 * len(kept) / len(mine)) if mine else None
    return {"profile": get_search_profile(db), "board_yield": stats,
            "apify_configured": bool(apify_token(db)), "actor_id": get_settings().apify_actor_id}


@router.put("/api/search/profile")
def search_profile_put(payload: SearchProfileIn, db: Session = Depends(get_user_db)):
    validate_search_profile(payload)
    profile = get_profile(db)
    prefs_value = dict(profile.search_preferences or {})
    prefs_value["search_profile"] = payload.model_dump()
    profile.search_preferences = prefs_value
    db.commit()
    return {"profile": get_search_profile(db)}


@router.post("/api/search/profile/preview")
def search_profile_preview(payload: SearchProfileIn):
    """Show exactly which actor runs a profile would start, with the inputs and any filter conflicts."""
    validate_search_profile(payload)
    try:
        runs, warnings = plan_runs(payload.model_dump())
    except ValueError as exc:
        return {"runs": [], "warnings": [], "error": str(exc)}
    return {"runs": [{"actor_id": r["actor_id"], "actor_name": r["actor_name"], "location": r["location"],
                      "roles": r["roles"], "input": r["input"]} for r in runs],
            "warnings": warnings, "error": None}


def run_search_profile(db: Session, sp: dict) -> list[SearchRun]:
    try:
        plan, _ = plan_runs(sp)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return [start_apify_search(db, {"actor_id": item["actor_id"], "actor_input": item["input"],
                                    "location": item["location"]}) for item in plan]


@router.post("/api/search/profile/run")
def search_profile_run(db: Session = Depends(get_user_db)):
    sp = get_search_profile(db)
    try:
        runs = run_search_profile(db, sp)
        _, warnings = plan_runs(sp)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"runs": [{"id": r.id, "status": r.status, "actor_id": getattr(r, "actor_id", ""),
                      "location": (r.input_json or {}).get("location", "")} for r in runs], "warnings": warnings}


@router.get("/api/search/apify/runs/{run_id}/detail")
def run_detail(run_id: int, db: Session = Depends(get_user_db)):
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    pulled = db.scalars(select(Job).where(Job.search_run_id == run.id).order_by(desc(Job.fit_score))).all()
    i = run.input_json or {}
    duration = ""
    if run.completed_at:
        seconds = int((run.completed_at - run.created_at).total_seconds())
        duration = f"{seconds // 60}m {seconds % 60:02d}s"
    filtered = sum(1 for j in pulled if j.status in ("archived", "dismissed") or is_agency(j.company))
    context = [["Actor", f"{run.actor_id} · run {run.external_run_id[:6]}…" + (f" · {duration}" if duration else "")],
               *describe_input(i)]
    if run.error:
        context.append(["Error", run.error])
    return {
        "id": run.id, "status": run.status, "created_at": run.created_at.isoformat(), "error": run.error,
        "completed": run.completed_at is not None,
        "counts": {"pulled": run.item_count, "new": run.created_jobs,
                   "duplicates": max(0, run.item_count - run.created_jobs), "filtered": filtered},
        "context": context,
        "results": [{**job_card(j), "verdict": "agency" if is_agency(j.company) else
                     "below fit floor" if j.status == "archived" else "dismissed" if j.status == "dismissed" else "new"}
                    for j in pulled[:50]],
    }


# ───────────────────────────── tailor / approvals / apply kit ─────────────────────────────

def keyword_coverage(job: Job, resume: ResumeVariant | None) -> list[dict]:
    analysis = job.fit_analysis or {}
    text = (resume.markdown.split("## Source Resume")[0] if resume else "").lower()
    gaps = " ".join(str(g) for g in (analysis.get("gaps") or []) + (analysis.get("must_have_missing") or [])).lower()
    words = list(dict.fromkeys([*(analysis.get("keywords") or []), *(analysis.get("strong_matches") or [])]))[:14]
    out = []
    for word in words:
        status = "covered" if str(word).lower() in text else "partial" if str(word).lower() in gaps else "missing"
        out.append({"word": str(word), "status": status})
    return out


# Status filters for the Tailor and Approvals screens: filter name -> test on a row.
TAILOR_STATUS_FILTERS = {
    "not_tailored": lambda r: not r["has_resume"],
    "has_packet": lambda r: r["has_resume"] and r["status"] != "pending_approval",
    "sent_back": lambda r: r["status"] == "pending_approval",
    "untailored": lambda r: r["untailored"],  # packet uses the master resume unchanged
}
APPROVAL_STATUS_FILTERS = {
    "waiting": lambda r: r["status"] == "tailored",
    "sent_back": lambda r: r["status"] in ("pending_approval", "rejected"),
    "approved": lambda r: r["status"] == "approved",
}
PACKET_SORTS = {
    "fit": lambda r: (r["fit"] is None, -(r["fit"] or 0), -r["id"]),
    "reviewer": lambda r: (r["reviewer_score"] is None, -(r["reviewer_score"] or 0), -r["id"]),
    "company": lambda r: (r["company"].lower(), -r["id"]),
    "new": lambda r: (r["discovered_at"],),  # reversed below
}


def filter_packet_rows(rows: list[dict], *, q: str, min_fit: int, min_review: int, board: str, status: str,
                       sort: str, status_filters: dict, with_facets: bool):
    """Filter and sort Tailor or Approvals rows. The unfiltered order is kept for sort="" (the screen's default).

    Returns the plain list unless `with_facets`, which adds totals and the boards present before filtering.
    """
    if status not in ("all", "") and status not in status_filters:
        raise HTTPException(422, f"Unknown status filter: {status}")
    if sort not in ("", "default") and sort not in PACKET_SORTS:
        raise HTTPException(422, f"Unknown sort: {sort}")
    needle = q.lower().strip()
    out = [r for r in rows
           if (not needle or needle in f"{r['title']} {r['company']} {r['location']}".lower())
           and (not min_fit or (r["fit"] or 0) >= min_fit)
           and (not min_review or (r["reviewer_score"] or 0) >= min_review)
           and (board in ("all", "") or r["board"] == board)
           and (status in ("all", "") or status_filters[status](r))]
    if sort in PACKET_SORTS:
        out.sort(key=PACKET_SORTS[sort], reverse=sort == "new")
    if not with_facets:
        return out
    return {"items": out, "total": len(out), "total_all": len(rows), "boards": sorted({r["board"] for r in rows})}


@router.get("/api/tailor/queue")
def tailor_queue(q: str = "", min_fit: int = 0, min_review: int = 0, board: str = "all", status: str = "all",
                 sort: str = "fit", with_facets: bool = False, db: Session = Depends(get_user_db)):
    # AI-ranked jobs only reach Tailor once you have confirmed the shortlist in triage.
    jobs = db.scalars(select(Job).where(or_(and_(Job.status == "shortlisted", Job.triaged_at.is_not(None)),
                                            Job.status.in_(("tailored", "pending_approval"))))
                      .order_by(desc(Job.fit_score))).all()
    # The reviewer score and ATS keyword coverage of each job's latest packet, so the queue shows
    # everything the single-job screen shows.
    latest: dict[int, ResumeVariant] = {}
    if jobs:
        for variant in db.scalars(select(ResumeVariant).where(ResumeVariant.job_id.in_([j.id for j in jobs]))
                                  .order_by(ResumeVariant.version)).all():
            latest[variant.job_id] = variant
    rows = []
    for job in jobs:
        resume = latest.get(job.id)
        coverage = keyword_coverage(job, resume) if resume else []
        covered = sum(1 for word in coverage if word["status"] == "covered")
        rows.append({**job_card(job), "has_resume": resume is not None,
                     "reviewer_score": resume.reviewer_score if resume else None,
                     "ats_pct": round(100 * covered / len(coverage)) if coverage else None,
                     "untailored": bool((resume.content or {}).get("untailored")) if resume else False,
                     "resume_version": resume.version if resume else None})
    return filter_packet_rows(rows, q=q, min_fit=min_fit, min_review=min_review, board=board, status=status, sort=sort,
                              status_filters=TAILOR_STATUS_FILTERS, with_facets=with_facets)


def packet(db: Session, job: Job) -> dict:
    resume = latest_resume(db, job.id)
    row = setting_row(db)
    base = {"job": job_card(job), "min_review_score": row.min_review_score,
            "application_mode": row.application_mode, "resume": None}
    if not resume:
        return base
    edits = resume_edits(db, resume)
    coverage = keyword_coverage(job, resume)
    covered = sum(1 for k in coverage if k["status"] == "covered")
    notes = resume.reviewer_notes or {}
    review = ([{"tone": "good", "text": s} for s in notes.get("strengths") or []]
              + [{"tone": "warn", "text": s} for s in notes.get("improvements") or []]
              + [{"tone": "bad", "text": s} for s in notes.get("reject_reasons") or []])
    return {**base, "resume": {
        "id": resume.id, "version": resume.version, "created_at": resume.created_at.isoformat(),
        "generated": ago(resume.created_at), "reviewer_score": resume.reviewer_score, "markdown": resume.markdown,
        "untailored": bool((resume.content or {}).get("untailored")),
        "cover_letter": resume.cover_letter, "cover_words": len(resume.cover_letter.split()),
        "edits": edits, "review": review, "keywords": coverage,
        "ats_pct": round(100 * covered / len(coverage)) if coverage else None,
    }}


@router.get("/api/tailor/{job_id}")
def tailor_detail(job_id: int, db: Session = Depends(get_user_db)):
    return packet(db, get_job(db, job_id))


class EditIn(BaseModel):
    status: str | None = None
    text: str | None = Field(default=None, max_length=4000)


@router.patch("/api/resumes/{resume_id}/edits/{index}")
def edit_resume(resume_id: int, index: int, payload: EditIn, db: Session = Depends(get_user_db)):
    resume = db.get(ResumeVariant, resume_id)
    if not resume:
        raise HTTPException(404, "Resume not found")
    if payload.status and payload.status not in ("accepted", "review", "reverted"):
        raise HTTPException(422, "Status must be accepted, review or reverted")
    if index < 0 or index >= len(resume_edits(db, resume)):
        raise HTTPException(404, "Edit not found")
    update_resume_edit(db, resume, index, payload.status, payload.text)
    return packet(db, resume.job)


@router.put("/api/resumes/{resume_id}/cover-letter")
def edit_cover_letter(resume_id: int, payload: EditIn, db: Session = Depends(get_user_db)):
    resume = db.get(ResumeVariant, resume_id)
    if not resume:
        raise HTTPException(404, "Resume not found")
    resume.cover_letter = (payload.text or "").strip()
    db.commit()
    return {"ok": True}


def answers_for(db: Session) -> list[KnowledgeAnswer]:
    return db.scalars(select(KnowledgeAnswer).order_by(desc(KnowledgeAnswer.verified), desc(KnowledgeAnswer.use_count))).all()


@router.get("/api/approvals")
def approvals(q: str = "", min_fit: int = 0, min_review: int = 0, board: str = "all", status: str = "all",
              sort: str = "default", with_facets: bool = False, db: Session = Depends(get_user_db)):
    jobs = db.scalars(select(Job).where(Job.status.in_(("tailored", "pending_approval", "approved", "rejected")))
                      .order_by(desc(Job.id))).all()
    rows = []
    for job in jobs:
        resume = latest_resume(db, job.id)
        app = job.application
        age = (f"approved {day_label(app.approved_at)}" if job.status == "approved" and app and app.approved_at
               else "sent back" if job.status in ("pending_approval", "rejected") else f"tailored {ago(resume.created_at) if resume else ''}")
        rows.append({**job_card(job), "reviewer_score": resume.reviewer_score if resume else None, "age": age,
                     "waiting": job.status == "tailored"})
    rows.sort(key=lambda r: (not r["waiting"], r["status"] == "approved"))
    return filter_packet_rows(rows, q=q, min_fit=min_fit, min_review=min_review, board=board, status=status, sort=sort,
                              status_filters=APPROVAL_STATUS_FILTERS, with_facets=with_facets)


@router.get("/api/approvals/{job_id}")
def approval_detail(job_id: int, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    data = packet(db, job)
    resume = data["resume"]
    profile = get_profile(db)
    answers = answers_for(db)
    checklist = []
    if resume:
        edits = [e for e in resume["edits"] if e["status"] != "reverted"]
        cited = sum(1 for e in edits if e["evidence"])
        rejects = (latest_resume(db, job.id).reviewer_notes or {}).get("reject_reasons") or []
        body = resume["markdown"].split("## Source Resume")[0]
        comp = [a for a in answers if a.category == "compensation"]
        score = resume["reviewer_score"] or 0
        checklist = [
            {"label": "Every claim cites evidence", "detail": f"{cited} of {len(edits)} edits traced", "state": "ok" if cited == len(edits) else "warn"},
            {"label": "No invented experience", "detail": rejects[0] if rejects else "Reviewer raised no unsupported claims", "state": "warn" if rejects else "ok"},
            {"label": "Contact details present", "detail": "Email and phone on profile" if profile.email and profile.phone else "Add email and phone in Settings", "state": "ok" if profile.email and profile.phone else "warn"},
            {"label": "One page, ATS-safe", "detail": f"{len(body.split())} words · plain text sections", "state": "ok" if len(body.split()) <= 650 else "warn"},
            {"label": "Compensation answer", "detail": "Verified answer on file" if any(a.verified for a in comp) else "No verified compensation answer yet", "state": "ok" if any(a.verified for a in comp) else "warn"},
            {"label": "Reviewer threshold", "detail": f"{score:.0f} vs minimum {data['min_review_score']}", "state": "ok" if score >= data["min_review_score"] else "warn"},
        ]
    app = job.application
    return {**data, "checklist": checklist, "notes": app.notes if app else "", "application_id": app.id if app else None,
            "profile": {"name": profile.name, "headline": profile.headline, "location": profile.location},
            "answers": [{"id": a.id, "question": a.question, "answer": a.answer, "verified": a.verified,
                         "sensitive": a.sensitive, "category": a.category} for a in answers[:6]]}


class NotesIn(BaseModel):
    notes: str = ""


@router.post("/api/jobs/{job_id}/revision")
def send_back(job_id: int, payload: NotesIn, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    if not job.resumes:
        raise HTTPException(400, "There is no tailored packet to send back")
    app = job.application or Application(job_id=job.id)
    db.add(app)
    app.status = "pending_approval"
    app.notes = payload.notes
    job.status = "pending_approval"
    db.commit()
    return {"ok": True, "status": job.status}


@router.put("/api/jobs/{job_id}/notes")
def save_notes(job_id: int, payload: NotesIn, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    app = job.application
    if not app:
        app = Application(job_id=job.id, status="pending_approval")
        db.add(app)
    app.notes = payload.notes
    db.commit()
    return {"ok": True}


@router.get("/api/jobs/{job_id}/apply-kit")
def apply_kit(job_id: int, db: Session = Depends(get_user_db)):
    job = get_job(db, job_id)
    app = job.application
    if not app or app.status not in ("approved", "applied"):
        raise HTTPException(400, "Approve the packet before opening the apply kit")
    resume = db.get(ResumeVariant, app.approved_resume_id) if app.approved_resume_id else latest_resume(db, job.id)
    profile = get_profile(db)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{profile.name or 'resume'} {job.company}".lower()).strip("-")
    answers = [a for a in answers_for(db) if a.answer and a.verified][:8]
    return {
        "job": job_card(job), "status": app.status, "reviewer_score": resume.reviewer_score if resume else None,
        "followup_date": (utcnow() + timedelta(days=followup_days(db))).isoformat(),
        "files": [
            {"kind": "PDF", "name": f"{slug}-v{resume.version}.pdf", "meta": "ATS-safe export", "href": f"/api/jobs/{job.id}/resume.pdf"},
            {"kind": "DOCX", "name": f"{slug}-v{resume.version}.docx", "meta": "for portals that reject PDF", "href": f"/api/jobs/{job.id}/resume.docx"},
            {"kind": "TXT", "name": f"cover-letter-{slug}.txt", "meta": f"{len(resume.cover_letter.split())} words · paste-ready", "href": f"/api/jobs/{job.id}/cover-letter.txt"},
        ] if resume else [],
        "answers": [{"id": a.id, "question": a.question, "answer": a.answer, "sensitive": a.sensitive} for a in answers],
    }


@router.post("/api/knowledge/{item_id}/used")
def knowledge_used(item_id: int, db: Session = Depends(get_user_db)):
    item = db.get(KnowledgeAnswer, item_id)
    if not item:
        raise HTTPException(404, "Knowledge answer not found")
    item.use_count = (item.use_count or 0) + 1
    db.commit()
    return {"use_count": item.use_count}


@router.get("/api/jobs/{job_id}/resume.docx")
def resume_docx(job_id: int, db: Session = Depends(get_user_db)):
    resume = latest_resume(db, job_id)
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    from docx import Document

    document = Document()
    for line in resume.markdown.split("## Source Resume")[0].splitlines():
        if line.startswith("# "):
            document.add_heading(line[2:], level=0)
        elif line.startswith("### "):
            parts = [p.strip() for p in line[4:].split("|")]
            paragraph = document.add_paragraph()
            paragraph.add_run(parts[0]).bold = True
            if len(parts) > 1:
                paragraph.add_run("  |  " + "  |  ".join(parts[1:]))
        elif line.startswith("## "):
            document.add_heading(line[3:], level=1)
        elif line.startswith("- "):
            document.add_paragraph(line[2:], style="List Bullet")
        elif line.strip():
            document.add_paragraph(line)
    buffer = BytesIO()
    document.save(buffer)
    return Response(buffer.getvalue(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="job-{job_id}-resume.docx"'})


@router.get("/api/jobs/{job_id}/cover-letter.txt")
def cover_letter_txt(job_id: int, db: Session = Depends(get_user_db)):
    resume = latest_resume(db, job_id)
    if not resume:
        raise HTTPException(404, "No tailored resume for this job")
    return Response(resume.cover_letter, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="job-{job_id}-cover-letter.txt"'})


# ───────────────────────────── tracker ─────────────────────────────

def stages(db: Session) -> list[dict]:
    extras = prefs(db).get("extra_stages") or []
    return [*BASE_STAGES, *[s for s in EXTRA_STAGES if s["id"] in extras], CLOSED_STAGE]


def app_payload(db: Session, app: Application) -> dict:
    job = app.job
    now = utcnow()
    draft = db.scalar(select(FollowUp).where(FollowUp.application_id == app.id).order_by(desc(FollowUp.id)).limit(1))
    resume = db.get(ResumeVariant, app.approved_resume_id) if app.approved_resume_id else None
    stage = app.stage or "applied"
    tag, tone = "", "muted"
    if stage == "applied" and app.followup_due_at:
        delta = (app.followup_due_at.date() - now.date()).days
        tag, tone = ("follow-up due" if delta <= 0 else f"follow-up {day_label(app.followup_due_at)}"), ("warn" if delta <= 0 else "muted")
    upcoming = next((e for e in app.events if e.kind == "interview" and e.occurred_at >= now), None)
    if upcoming:
        tag, tone = f"{upcoming.title.lower()} {day_label(upcoming.occurred_at)}", "good"
    if stage == "applied" and app.followup_due_at:
        next_step = ("Send the follow-up", f"due {day_label(app.followup_due_at)}")
    elif upcoming:
        next_step = (f"Prepare for {upcoming.title.lower()}",
                     format_in_zone(upcoming.occurred_at, event_meta(upcoming.meta)["timezone"]))
    elif stage in INACTIVE_STAGES:
        next_step = ("Archive or reopen", "")
    else:
        next_step = ("Log the latest contact", "keeps the timeline honest")
    return {
        "id": app.id, "job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
        "url": job.url, "fit": None if job.fit_score is None else round(job.fit_score), "stage": stage,
        "age": ago(app.applied_at), "tag": tag, "tone": tone,
        "meta": f"applied {day_label(app.applied_at)}" + (f" · follow-up {day_label(app.followup_due_at)}" if app.followup_due_at and stage == "applied" else ""),
        "recruiter_name": app.recruiter_name, "recruiter_email": app.recruiter_email, "notes": app.notes,
        "recruiters": recruiters_of(app),
        "docs": " · ".join(filter(None, [f"resume v{resume.version}" if resume else "", "cover letter" if resume and resume.cover_letter else ""])) or "not recorded",
        "draft": draft.draft if draft else "", "draft_id": draft.id if draft else None,
        "next": next_step[0], "next_date": next_step[1],
        "timeline": [{"id": e.id, "kind": e.kind, "title": e.title, "detail": e.detail,
                      "at": e.occurred_at.isoformat(), **event_meta(e.meta),
                      "at_local": format_in_zone(e.occurred_at, event_meta(e.meta)["timezone"], "%-d %b %Y, %H:%M")
                      if event_meta(e.meta)["timezone"] else ""} for e in app.events],
    }


@router.get("/api/tracker")
def tracker(db: Session = Depends(get_user_db)):
    apps = db.scalars(select(Application).where(Application.applied_at.is_not(None)).order_by(desc(Application.applied_at))).all()
    payload = [app_payload(db, a) for a in apps]
    live = [a for a in payload if a["stage"] not in INACTIVE_STAGES]
    replies = [a for a in payload if a["stage"] in REPLY_STAGES]
    due = [a for a in apps if (a.stage or "applied") == "applied" and a.followup_due_at and a.followup_due_at <= utcnow()]
    extras = prefs(db).get("extra_stages") or []
    return {"stages": stages(db), "available_stages": [s for s in EXTRA_STAGES if s["id"] not in extras],
            "extra_stages": extras, "applications": payload,
            "stats": {"live": len(live), "replies": len(replies), "due": len(due),
                      "response_rate": _pct(len(replies), len(payload)) if payload else "—"}}


class StagesIn(BaseModel):
    extra_stages: list[str] = Field(default_factory=list)


@router.put("/api/tracker/stages")
def tracker_stages(payload: StagesIn, db: Session = Depends(get_user_db)):
    valid = {s["id"] for s in EXTRA_STAGES}
    if set(payload.extra_stages) - valid:
        raise HTTPException(422, "Unknown stage")
    row = setting_row(db)
    current = dict(row.preferences or {})
    current["extra_stages"] = list(dict.fromkeys(payload.extra_stages))
    row.preferences = current
    db.commit()
    return tracker(db)


class StageIn(BaseModel):
    stage: str


def get_app(db: Session, application_id: int) -> Application:
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@router.post("/api/applications/{application_id}/stage")
def move_stage(application_id: int, payload: StageIn, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if payload.stage not in ALL_STAGE_IDS:
        raise HTTPException(422, "Unknown stage")
    if not app.applied_at:
        raise HTTPException(400, "Only submitted applications can move through the tracker")
    if app.stage != payload.stage:
        label = next(s["label"] for s in [*BASE_STAGES, *EXTRA_STAGES, CLOSED_STAGE] if s["id"] == payload.stage)
        db.add(ApplicationEvent(application=app, kind="stage", title=f"Moved to {label}", detail=payload.stage))
        app.stage = payload.stage
        db.commit()
    return app_payload(db, app)


class EventIn(BaseModel):
    kind: str = "note"
    title: str = Field(default="", max_length=300)
    detail: str = Field(default="", max_length=4000)
    occurred_at: datetime | None = None
    # Interviews: a wall-clock time in a named IANA time zone (e.g. "2026-09-16T16:00" in "Asia/Kolkata"),
    # used instead of occurred_at, plus an optional contact.
    local_time: datetime | None = None
    timezone: str = Field(default="", max_length=64)
    contact_name: str = Field(default="", max_length=200)
    contact_email: str = Field(default="", max_length=300)
    contact_phone: str = Field(default="", max_length=40)

    @field_validator("contact_email")
    @classmethod
    def _email(cls, value: str) -> str:
        return clean_email(value)

    @field_validator("contact_phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return clean_phone(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            zone(value)
        return value

    @model_validator(mode="after")
    def _local_time_needs_zone(self) -> EventIn:
        if self.local_time is not None and self.local_time.tzinfo is None and not self.timezone:
            raise ValueError("A local time needs a time zone")
        return self


@router.post("/api/applications/{application_id}/events")
def add_event(application_id: int, payload: EventIn, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if payload.kind not in ("note", "call", "email", "referral", "interview"):
        raise HTTPException(422, "Unknown event kind")
    if not (payload.title.strip() or payload.detail.strip()):
        raise HTTPException(422, "Write something to log")
    if payload.local_time is not None:
        occurred = to_utc(payload.local_time, payload.timezone)
    elif payload.occurred_at is not None:
        occurred = (payload.occurred_at.astimezone(UTC).replace(tzinfo=None) if payload.occurred_at.tzinfo
                    else payload.occurred_at)
    else:
        occurred = utcnow()
    meta = {key: value for key, value in (("timezone", payload.timezone), ("contact_name", payload.contact_name.strip()),
                                          ("contact_email", payload.contact_email),
                                          ("contact_phone", payload.contact_phone)) if value}
    db.add(ApplicationEvent(application=app, kind=payload.kind, title=payload.title.strip() or payload.kind.title(),
                            detail=payload.detail.strip(), occurred_at=occurred, meta=meta or None))
    if payload.kind == "interview" and (app.stage or "applied") in ("applied", "acknowledged", "screening"):
        db.add(ApplicationEvent(application=app, kind="stage", title="Moved to Interviewing", detail="interviewing"))
        app.stage = "interviewing"
    db.commit()
    db.refresh(app)
    return app_payload(db, app)


class RecruiterPatchIn(BaseModel):
    recruiter_name: str = ""
    recruiter_email: str = ""


@router.put("/api/applications/{application_id}/recruiter")
def set_recruiter(application_id: int, payload: RecruiterPatchIn, db: Session = Depends(get_user_db)):
    """Set the first recruiter's name and email. Other recruiters are kept."""
    app = get_app(db, application_id)
    set_primary_recruiter(app, payload.recruiter_name, payload.recruiter_email)
    db.commit()
    return app_payload(db, app)


class RecruitersIn(BaseModel):
    recruiters: list[ContactIn] = Field(default_factory=list, max_length=MAX_RECRUITERS)


@router.put("/api/applications/{application_id}/recruiters")
def replace_recruiters(application_id: int, payload: RecruitersIn, db: Session = Depends(get_user_db)):
    """Replace every recruiter on an application, in order. The first is the primary contact."""
    app = get_app(db, application_id)
    set_recruiters(app, [r.model_dump() for r in payload.recruiters])
    db.commit()
    return app_payload(db, app)


@router.post("/api/applications/{application_id}/followup-draft")
def followup_draft(application_id: int, db: Session = Depends(get_user_db)):
    app = get_app(db, application_id)
    if not app.applied_at:
        raise HTTPException(400, "Follow-ups are only drafted for submitted applications")
    try:
        draft_followup(db, app)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return app_payload(db, app)


# ───────────────────────────── settings helpers ─────────────────────────────

class PreferencesIn(BaseModel):
    weekly_target: int = Field(default=10, ge=1, le=50)
    followup_days: int = Field(default=7, ge=1, le=60)
    email_run_sheet: bool = False
    warn_closing: bool = True


@router.get("/api/preferences")
def preferences_get(db: Session = Depends(get_user_db)):
    config = resolve_ai_config(get_settings(), (setting_row(db).preferences or {}).get("ai") or {})
    provider = config["llm_provider"]
    stage_models = []
    for stage, label in AI_STAGES:
        model = "mock (no API calls)" if provider == "mock" else config["stage_models"][stage]
        stage_models.append({"id": stage, "stage": label, "model": model, "source": config["stage_sources"][stage],
                             "badge": "free" if is_free_model(provider, model) else "paid"})
    return {**prefs(db), "stage_models": stage_models, "require_free": config["require_free"]}


@router.put("/api/preferences")
def preferences_put(payload: PreferencesIn, db: Session = Depends(get_user_db)):
    row = setting_row(db)
    row = lock_settings_row(db, row)
    row.preferences = {**(row.preferences or {}), **payload.model_dump(exclude_unset=True)}
    db.commit()
    return preferences_get(db)


@router.get("/api/profile/resumes")
def resume_list(db: Session = Depends(get_user_db)):
    docs = db.scalars(select(ResumeDocument).order_by(desc(ResumeDocument.active), desc(ResumeDocument.id))).all()
    # in_use says whether tailoring actually uses this file's text: the master resume text can be
    # edited by hand in Settings, and only the text is sent to the model.
    master = (get_profile(db).master_resume or "").strip()
    return [{"id": d.id, "filename": d.original_filename, "size": d.size, "media_type": d.media_type,
             "active": d.active, "created_at": d.created_at.isoformat(), "words": len(d.extracted_text.split()),
             "in_use": (d.extracted_text or "").strip() == master} for d in docs]


@router.post("/api/profile/resumes/{document_id}/primary")
def resume_primary(document_id: int, db: Session = Depends(get_user_db)):
    document = db.get(ResumeDocument, document_id)
    if not document:
        raise HTTPException(404, "Resume not found")
    for other in db.scalars(select(ResumeDocument).where(ResumeDocument.active.is_(True))).all():
        other.active = False
    document.active = True
    get_profile(db).master_resume = document.extracted_text
    db.commit()
    return resume_list(db)


@router.delete("/api/profile/resumes/{document_id}")
def resume_document_delete(document_id: int, db: Session = Depends(get_user_db)):
    """Delete an uploaded resume. Deleting the primary one promotes the next newest and uses its text."""
    document = db.get(ResumeDocument, document_id)
    if not document:
        raise HTTPException(404, "Resume not found")
    was_primary, path = document.active, Path(document.storage_path)
    db.delete(document)
    db.flush()
    promoted = None
    if was_primary:
        promoted = db.scalars(select(ResumeDocument).order_by(desc(ResumeDocument.id)).limit(1)).first()
        if promoted:
            promoted.active = True
            get_profile(db).master_resume = promoted.extracted_text
    db.commit()
    # Uploads of identical files share one stored file, so only remove it when no row still points at it.
    if path.is_file() and not db.scalar(select(ResumeDocument.id).where(ResumeDocument.storage_path == str(path))):
        path.unlink(missing_ok=True)
    return {"deleted": document_id, "promoted": promoted.id if promoted else None, "resumes": resume_list(db)}


@router.post("/api/profile/resume/parse-evidence")
def parse_evidence(db: Session = Depends(get_user_db)):
    profile = get_profile(db)
    if not profile.master_resume.strip():
        raise HTTPException(400, "Upload or paste a master resume first")
    try:
        result = LLMClient.for_user(db).generate_prompt_json(build_evidence_extraction_prompt(profile.master_resume),
                                                            LLMCall(workflow_type="profile", entity_id=profile.id))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    existing = {e.claim.strip().lower() for e in db.scalars(select(Evidence)).all()}
    added = 0
    for item in result.get("claims") or []:
        claim = str(item.get("claim", "")).strip() if isinstance(item, dict) else ""
        if not claim or claim.lower() in existing:
            continue
        # Parsed claims start unverified: only a human can make them eligible for tailoring.
        db.add(Evidence(claim=claim[:2000], company=str(item.get("company", ""))[:200], role=str(item.get("role", ""))[:200],
                        skills=[str(s) for s in item.get("skills") or []][:12], verified=False))
        existing.add(claim.lower())
        added += 1
    db.commit()
    return {"added": added}


class EvidencePatchIn(BaseModel):
    verified: bool


@router.patch("/api/evidence/{evidence_id}")
def evidence_patch(evidence_id: int, payload: EvidencePatchIn, db: Session = Depends(get_user_db)):
    item = db.get(Evidence, evidence_id)
    if not item:
        raise HTTPException(404, "Evidence not found")
    item.verified = payload.verified
    db.commit()
    return {"id": item.id, "verified": item.verified}


@router.delete("/api/evidence/{evidence_id}")
def evidence_delete(evidence_id: int, db: Session = Depends(get_user_db)):
    item = db.get(Evidence, evidence_id)
    if not item:
        raise HTTPException(404, "Evidence not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.post("/api/search/apify/runs/{run_id}/rank", status_code=202)
def rank_run_jobs(run_id: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_user_db)):
    """Queue the unranked jobs a run pulled in. Imports already queue new jobs; this only catches older ones."""
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(404, "Search run not found")
    ids = db.scalars(select(Job.id).where(Job.search_run_id == run.id, Job.fit_score.is_(None),
                                          Job.state.in_(ranker.RANK_WAITING_STATES))).all()
    out = ranker.request_ranking(db, ids, force=False, only_unranked=True, actor=f"user:{db.info['user_id']}")
    run_soon(background, request.app, "ranking")
    return out


class KnowledgeUpdateIn(BaseModel):
    question: str = Field(min_length=2)
    answer: str = ""
    category: str = "general"
    answer_type: str = "text"
    verified: bool = False
    sensitive: bool = False


@router.put("/api/knowledge/{item_id}")
def knowledge_put(item_id: int, payload: KnowledgeUpdateIn, db: Session = Depends(get_user_db)):
    item = db.get(KnowledgeAnswer, item_id)
    if not item:
        raise HTTPException(404, "Knowledge answer not found")
    key = hashlib.sha256(re.sub(r"[^a-z0-9]+", " ", payload.question.lower()).strip().encode()).hexdigest()
    clash = db.scalar(select(KnowledgeAnswer).where(KnowledgeAnswer.question_key == key, KnowledgeAnswer.id != item.id))
    if clash:
        raise HTTPException(409, "Another saved answer already covers this question")
    for field, value in payload.model_dump().items():
        setattr(item, field, value)
    item.question_key = key
    db.commit()
    return {"id": item.id, "verified": item.verified}


# ───────────────────────────── Apify run sync ─────────────────────────────

def _apify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return "Apify rejected the token" if code in (401, 403) else "Apify run not found" if code == 404 else f"Apify returned HTTP {code}"
    if isinstance(exc, httpx.HTTPError):
        return f"Could not reach Apify ({exc.__class__.__name__})"
    return str(exc)


@router.get("/api/search/apify/remote-runs")
def remote_runs(limit: int = Query(20, ge=1, le=50), db: Session = Depends(get_user_db)):
    """Runs in the connected Apify account, marked with whether Job Hunt OS has imported them."""
    try:
        return {"runs": list_remote_runs(db, limit)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, _apify_error(exc)) from exc


class ImportRunsIn(BaseModel):
    external_run_ids: list[str] = Field(min_length=1, max_length=25)
    reimport: bool = False


def _sync_results(db: Session, work) -> dict:
    results = []
    for label, action in work:
        try:
            results.append({**action(), "ok": True})
        except Exception as exc:  # one bad run must not stop the others
            db.rollback()
            results.append({"external_run_id": label, "ok": False, "error": _apify_error(exc)})
    ok = [r for r in results if r["ok"]]
    return {"results": results, "created": sum((r.get("import") or {}).get("created", 0) for r in ok),
            "duplicates": sum((r.get("import") or {}).get("duplicates", 0) for r in ok)}


@router.post("/api/search/apify/import")
def import_runs(payload: ImportRunsIn, db: Session = Depends(get_user_db)):
    """Import runs by Apify run ID: track them, normalize their items and skip duplicates."""
    if not apify_token(db):
        raise HTTPException(400, "Add your Apify API token in Settings")
    bad = [rid for rid in payload.external_run_ids if not re.fullmatch(r"[A-Za-z0-9]{6,40}", rid)]
    if bad:
        raise HTTPException(422, f"Invalid Apify run IDs: {', '.join(bad[:3])}")
    work = [(rid, lambda rid=rid: import_remote_run(db, rid, payload.reimport)) for rid in dict.fromkeys(payload.external_run_ids)]
    return _sync_results(db, work)


@router.post("/api/search/apify/runs/refresh-pending")
def refresh_pending_runs(db: Session = Depends(get_user_db)):
    """Check every run that has not finished yet and import the ones that have."""
    if not apify_token(db):
        raise HTTPException(400, "Add your Apify API token in Settings")
    pending = db.scalars(select(SearchRun).where(SearchRun.provider == "apify", SearchRun.completed_at.is_(None))
                         .order_by(desc(SearchRun.id)).limit(25)).all()
    return _sync_results(db, [(run.external_run_id, lambda run=run: refresh_apify_run(db, run)) for run in pending])
