"""Is this job in the candidate's domain? Deterministic, no model: quality over quantity at discovery.

    new job → assess(job, rules)
      MATCH         → description recovery / parsing / ranking as usual
      NOT_MATCHING  → hidden (SKIPPED "Outside your domain: sales"), never recovered, parsed or ranked; listed in
                      Discovery → Not matching, where the person can keep it (KEPT, never re-assessed)

Rules come from the account: its target roles, headline and resume (roles and skills), the roles of its saved
searches, its own exclude and include lists (seeded with common off-domain areas), and terms learned from the jobs
it dismissed (app.services.feedback_learning). A term the candidate's own profile uses is never excluded, and an
include term always wins. When unsure, the job is kept: only a clear term match hides a job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobDescriptionFetchTask, JobDescriptionParseTask, JobRankingTask
from app.services.lifecycle import WorkflowState as S
from app.services.lifecycle import can_transition
from app.settings import get_settings

MATCH, NOT_MATCHING, KEPT = "MATCH", "NOT_MATCHING", "KEPT"
# Off-domain areas: excluded unless the candidate's own target roles name them.
DEFAULT_DOMAIN_EXCLUDES = ("sales", "account executive", "account manager", "business development", "bdr", "sdr",
                           "digital marketing", "marketing", "seo", "sem", "social media", "content writer",
                           "copywriter", "recruiter", "talent acquisition", "customer support", "customer service",
                           "telecaller", "telesales")
# Skill sets: excluded in a title that names none of the candidate's own skills.
DEFAULT_SKILL_EXCLUDES = ("c++", "java", ".net", "php", "frontend", "front-end", "front end", "ui developer",
                          "react developer", "angular", "ios", "android", "mobile developer", "qa", "test engineer",
                          "manual testing")
# Words in a role that say nothing about its domain.
GENERIC_ROLE_WORDS = frozenset({
    "engineer", "engineering", "developer", "development", "senior", "sr", "junior", "jr", "lead", "staff",
    "principal", "head", "manager", "director", "vp", "chief", "officer", "specialist", "associate", "intern",
    "architect", "consultant", "analyst", "scientist", "i", "ii", "iii", "iv", "of", "and", "the", "for", "with",
    "to", "in", "at", "a", "an", "remote", "hybrid", "team", "member", "expert", "professional", "executive"})
DESCRIPTION_WINDOW = 3000


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _norm(term: str) -> str:
    return re.sub(r"\s+", " ", (term or "").strip().lower())


def term_pattern(term: str) -> re.Pattern[str]:
    """A whole-word match that works for C++, .NET and "front-end": "java" never matches "javascript"."""
    return re.compile(rf"(?<![\w+#.]){re.escape(_norm(term))}(?![\w+#])")


def contains(text: str, term: str) -> bool:
    return bool(term) and term_pattern(term).search(_norm(text)) is not None


def role_tokens(roles: list[str]) -> set[str]:
    """The words of the candidate's roles that name a domain: "AI" and "backend" from "Senior Backend AI Engineer"."""
    return {w for role in roles for w in re.findall(r"[a-z0-9+#.]+", role.lower())
            if w not in GENERIC_ROLE_WORDS and len(w) > 1}


@dataclass
class Rules:
    enabled: bool = True
    domain_excludes: list[str] = field(default_factory=list)
    skill_excludes: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    learned_titles: list[dict[str, Any]] = field(default_factory=list)
    learned_companies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def role_words(self) -> set[str]:
        return role_tokens(self.roles)


@dataclass(frozen=True)
class Verdict:
    match: bool
    code: str  # MATCH | DOMAIN | SKILL | DESCRIPTION | LEARNED
    reason: str = ""


# ───────────── rules ─────────────

def preferences(db: Session, user_id: int) -> dict[str, Any]:
    """The account's saved domain settings with defaults: {enabled, exclude_terms, skill_excludes, include_terms,
    learn_from_feedback, learned}."""
    from app.models import AppSetting

    row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
    saved = ((row.preferences if row else None) or {}).get("domain") or {}
    return {"enabled": saved.get("enabled", True),
            "exclude_terms": list(saved.get("exclude_terms", DEFAULT_DOMAIN_EXCLUDES)),
            "skill_excludes": list(saved.get("skill_excludes", DEFAULT_SKILL_EXCLUDES)),
            "include_terms": list(saved.get("include_terms", [])),
            "learn_from_feedback": saved.get("learn_from_feedback", True),
            "learned": list(saved.get("learned", []))}


def derived(db: Session, user_id: int) -> dict[str, list[str]]:
    """Roles and skills the account's own data names: profile, active resume, saved searches (roles, primary skills)."""
    from app.models import CandidateProfile, Resume, SavedSearch

    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user_id))
    roles: list[str] = list((profile.target_roles if profile else None) or [])
    skills: list[str] = list(((profile.search_preferences or {}) if profile else {}).get("primary_skills") or [])
    if profile is not None and profile.headline:
        roles.append(profile.headline)
    resume = db.scalar(select(Resume).where(Resume.user_id == user_id, Resume.is_active.is_(True)))
    if resume is not None and resume.structured_data:
        from app.services.structured_resume import derive

        try:
            facts = derive(resume.structured_data)
        except Exception:  # noqa: BLE001  a half-parsed resume still leaves the profile's own roles
            facts = {}
        roles += facts.get("primary_roles") or []
        skills += facts.get("primary_skills") or []
    for setup in db.scalars(select(SavedSearch).where(SavedSearch.user_id == user_id)).all():
        roles += [r for r in ((setup.settings or {}).get("roles") or []) if isinstance(r, str)]
        skills += [s for s in ((setup.settings or {}).get("primary_skills") or []) if isinstance(s, str)]
    clean = lambda values: list(dict.fromkeys(_norm(v) for v in values if isinstance(v, str) and v.strip()))
    return {"roles": clean(roles), "skills": clean(skills)}


def rules_for(db: Session, user_id: int) -> Rules:
    prefs, own = preferences(db, user_id), derived(db, user_id)
    words = role_tokens(own["roles"])
    own_skills = set(own["skills"])
    # A term the candidate's own roles or skills use is never an exclusion ("sales engineer", "Java" on the resume).
    domain = [t for t in map(_norm, prefs["exclude_terms"])
              if t and not any(contains(role, t) for role in own["roles"])]
    skill = [t for t in map(_norm, prefs["skill_excludes"]) if t and t not in own_skills and t not in words]
    learned = [e for e in prefs["learned"] if e.get("status") == "active"] if prefs["learn_from_feedback"] else []
    return Rules(enabled=bool(prefs["enabled"]) and get_settings().domain_filter_enabled, domain_excludes=domain,
                 skill_excludes=skill, includes=[t for t in map(_norm, prefs["include_terms"]) if t],
                 roles=own["roles"], skills=own["skills"],
                 learned_titles=[e for e in learned if e.get("kind") == "title"],
                 learned_companies=[e for e in learned if e.get("kind") == "company"])


# ───────────── the verdict ─────────────

def assess(job: Any, rules: Rules) -> Verdict:
    """MATCH unless a clear rule says otherwise. Include terms always win."""
    title = _norm(getattr(job, "title", "") or "")
    if not rules.enabled or not title:
        return Verdict(True, MATCH)
    if any(contains(title, term) for term in rules.includes):
        return Verdict(True, MATCH, "Matches one of your include terms")
    hit = next((term for term in rules.domain_excludes if contains(title, term)), None)
    if hit:
        return Verdict(False, "DOMAIN", f"Outside your domain: {hit}")
    has_own_skill = any(contains(title, skill) for skill in rules.skills)
    has_role_word = any(contains(title, word) for word in rules.role_words)
    hit = next((term for term in rules.skill_excludes if contains(title, term)), None)
    if hit and not has_own_skill:
        return Verdict(False, "SKILL", f"Different skill set: {hit}")
    company = _norm(getattr(job, "company", "") or "")
    learned_company = next((e for e in rules.learned_companies if company and _norm(e.get("term", "")) == company), None)
    if learned_company:
        return Verdict(False, "LEARNED", f"You dismissed {learned_company.get('evidence', 0)} jobs at this company")
    learned = next((e for e in rules.learned_titles if contains(title, e.get("term", ""))), None)
    if learned and not has_own_skill:
        return Verdict(False, "LEARNED", f"Similar to jobs you dismissed: '{learned['term']}' "
                                         f"({learned.get('evidence', 0)}×)")
    if not has_own_skill and not has_role_word and rules.skills:
        body = _norm((getattr(job, "description", "") or "")[:DESCRIPTION_WINDOW])
        foreign = [term for term in rules.skill_excludes if contains(body, term)]
        if len(foreign) >= 2 and not any(contains(body, skill) for skill in rules.skills):
            return Verdict(False, "DESCRIPTION", f"Different skill set: {', '.join(foreign[:3])}")
    return Verdict(True, MATCH)


# ───────────── acting on it ─────────────

def _cancel_pending_work(db: Session, job: Job, now: datetime) -> None:
    """A hidden job spends nothing: waiting recovery, parse and ranking tasks are closed."""
    reason = "Hidden: outside your domain"
    for task in db.scalars(select(JobDescriptionFetchTask).where(JobDescriptionFetchTask.job_id == job.id,
                                                                JobDescriptionFetchTask.status == "PENDING")).all():
        task.status, task.result_code, task.completed_at, task.last_error = "SKIPPED", NOT_MATCHING, now, reason
    for task in db.scalars(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id,
                                                                JobDescriptionParseTask.status == "PENDING")).all():
        task.status, task.completed_at, task.last_error = "FAILED", now, reason
    for task in db.scalars(select(JobRankingTask).where(JobRankingTask.job_id == job.id,
                                                       JobRankingTask.status == "PENDING")).all():
        task.status, task.completed_at, task.last_error = "SKIPPED", now, reason
        task.locked_by = task.lock_token = task.lock_expires_at = None


def mark(job: Job, verdict: Verdict, now: datetime | None = None) -> None:
    job.domain_match = MATCH if verdict.match else NOT_MATCHING
    job.domain_reason = verdict.reason[:300] or None
    job.domain_checked_at = now or utcnow()


def hide(db: Session, job: Job, verdict: Verdict, *, actor: str, now: datetime | None = None) -> None:
    """Mark a job not matching and take it out of the pipeline. Only jobs not ranked yet are moved; a ranked job
    keeps its state (it is only marked). Reversible with keep()."""
    from app.services.work_queue import transition

    now = now or utcnow()
    mark(job, verdict, now)
    _cancel_pending_work(db, job, now)
    if job.state is S.DISCOVERED:
        transition(db, job, S.NORMALIZED, actor=actor, reason="Title and company present", now=now)
    if job.state in (S.NORMALIZED, S.FILTERED, S.RANK_PENDING):
        transition(db, job, S.SKIPPED, actor=actor, reason=verdict.reason, now=now)


def screen(db: Session, job: Job, rules_cache: dict[int, Rules] | None = None, *, actor: str = "discovery",
           now: datetime | None = None) -> bool:
    """Assess a job not assessed yet, hiding it when it does not match. True when it may carry on."""
    if job.domain_match in (MATCH, KEPT):
        return True
    if job.domain_match == NOT_MATCHING:
        return False
    cache = rules_cache if rules_cache is not None else {}
    if job.user_id not in cache:
        cache[job.user_id] = rules_for(db, job.user_id)
    verdict = assess(job, cache[job.user_id])
    if verdict.match:
        mark(job, verdict, now)
        return True
    hide(db, job, verdict, actor=actor, now=now)
    return False


def keep(db: Session, job: Job, *, actor: str, now: datetime | None = None) -> str | None:
    """The person keeps a hidden job: it is never assessed again and goes through recovery, parsing and ranking
    like any new job. Returns why ranking could not queue it, or None."""
    from app.services import jd_recovery, job_description, ranker
    from app.services.work_queue import transition

    now = now or utcnow()
    job.domain_match, job.domain_reason, job.domain_checked_at = KEPT, "Kept by you", now
    if job.state is S.SKIPPED and can_transition(job.state, S.NORMALIZED, previous_state=job.previous_state):
        transition(db, job, S.NORMALIZED, actor=actor, reason="Kept although it looked outside your domain", now=now)
    from app.services import discovery

    discovery.choose_resume(db, job)  # hidden jobs were never matched to a resume
    if not jd_recovery.on_new_job(db, job):
        job_description.queue_parse(db, job, reason="user_request", now=now)
    if job.state in ranker.PRE_RANK_STATES:
        return ranker.queue_job(db, job, policy=ranker.policy_for(db, job.user_id), actor=actor, now=now)
    return None


def reapply(db: Session, user_id: int, *, actor: str) -> dict[str, int]:
    """Assess the account's unranked jobs again after its lists changed. Kept jobs are never touched; a job that now
    matches is put back in the queue. Does not commit."""
    rules = rules_for(db, user_id)
    hidden = restored = 0
    jobs = db.scalars(select(Job).where(Job.user_id == user_id, Job.domain_match.is_distinct_from(KEPT),
                                        Job.state.in_((S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING,
                                                       S.SKIPPED)))).all()
    for job in jobs:
        was_hidden = job.domain_match == NOT_MATCHING
        if job.state is S.SKIPPED and not was_hidden:
            continue  # skipped for another reason
        verdict = assess(job, rules)
        if verdict.match and was_hidden:
            keep(db, job, actor=actor)
            job.domain_match, job.domain_reason = MATCH, None
            restored += 1
        elif verdict.match:
            mark(job, verdict)
        elif not was_hidden:
            hide(db, job, verdict, actor=actor)
            hidden += 1
        else:
            mark(job, verdict)
    return {"hidden": hidden, "restored": restored, "checked": len(jobs)}


def sweep(db: Session, limit: int = 200) -> dict[str, int]:
    """Assess not-yet-assessed jobs that have not been ranked (the backlog from before this feature). Commits."""
    jobs = db.scalars(select(Job).where(Job.domain_match.is_(None), Job.user_id.is_not(None),
                                        Job.state.in_((S.DISCOVERED, S.NORMALIZED, S.FILTERED, S.RANK_PENDING)))
                      .order_by(Job.id.desc()).limit(max(1, limit))).all()
    cache: dict[int, Rules] = {}
    hidden = sum(0 if screen(db, job, cache, actor="domain_filter") else 1 for job in jobs)
    db.commit()
    return {"assessed": len(jobs), "hidden": hidden}
