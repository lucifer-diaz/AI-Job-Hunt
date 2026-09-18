"""Task-specific LLM context: only the candidate facts a task needs, chosen deterministically.

    CandidateProfile + active Resume (structured, verified) + Evidence Bank + job
    → build_ranking_context / build_tailoring_context / build_review_context / build_followup_context
    → TaskContext (structured data, not text) → app.prompts renders it → LLM

Selection is plain application logic, no model call: a role, bullet, skill or evidence item is relevant when it
shares vocabulary with the posting (title, description, listed skills) or matches a theme the posting stresses
(leadership, AI, reliability, data, cloud). The most recent role is always kept, and every role header stays
so the model sees the whole career outline.

Verification rules:
* Tailoring, review and follow-ups get VERIFIED facts only (a bullet counts when it and its role are verified).
  With no verified facts at all, tailoring stops with UnverifiedResumeError, unless CandidatePolicy
  `allow_unverified_resume_facts` is on, in which case unverified facts are included and marked.
* Ranking may also read UNVERIFIED/EDITED facts when `ranking_uses_unverified` is on; they are kept apart as
  lower-confidence context. REJECTED facts never reach generation; the reviewer receives them to flag reuse.

Token awareness: every context records `full_available_context_tokens` (the whole resume text, all verified
evidence and the posting) against `selected_context_tokens` (what this context holds), and the reduction.
The numbers travel with the LLM call into LLMRequestLog (app.services.llm_telemetry).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import CandidateProfile, Evidence, Job, KnowledgeAnswer, Resume
from app.prompts.common import job_facts, profile_facts
from app.services import resume_verification as rv
from app.services import structured_resume as sr
from app.services.llm_telemetry import estimate_tokens
from app.services.resume_format import parse_resume
from app.services.user_settings import candidate_policy

VERIFIED_ONLY = ("VERIFIED",)
WITH_UNVERIFIED = ("VERIFIED", "UNVERIFIED", "EDITED")
THEMES = {
    "leadership": ("lead", "leadership", "manager", "management", "mentor", "mentoring", "hiring", "hired", "director",
                   "head", "roadmap", "stakeholder", "managed", "led"),
    "ai": ("ai", "ml", "machine", "learning", "llm", "llms", "genai", "generative", "nlp", "rag", "agents", "agentic",
           "model", "models", "inference", "langchain", "langgraph", "openai"),
    "reliability": ("reliability", "sre", "availability", "uptime", "incident", "incidents", "on-call", "observability",
                    "latency", "slo", "slos", "resilience", "monitoring", "production"),
    "data": ("data", "kafka", "spark", "airflow", "etl", "pipeline", "pipelines", "warehouse", "snowflake", "bigquery",
             "streaming", "flink", "dbt", "lakehouse"),
    "cloud": ("aws", "gcp", "azure", "cloud", "kubernetes", "k8s", "terraform", "serverless", "docker"),
}
BULLETS_PER_ROLE = 4
BULLETS_RECENT_ROLE = 6
EXTRA_SKILLS = 10
TAILOR_EVIDENCE = 20
REVIEW_EVIDENCE = 30
LETTER_EVIDENCE = 12
LETTER_KNOWLEDGE = 6
# Answers a letter may raise only because the posting does: the words that show the posting raised them.
KNOWLEDGE_TRIGGERS = ("visa", "sponsor", "sponsorship", "authorization", "authorisation", "work permit", "relocate",
                      "relocation", "notice", "start date", "availability", "onsite", "on-site", "hybrid", "remote")


class UnverifiedResumeError(ValueError):
    """Final application content needs verified facts and the candidate has none."""


@dataclass
class ContextStats:
    full_available_context_tokens: int = 0
    selected_context_tokens: int = 0
    context_reduction_percent: float = 0.0

    @classmethod
    def measure(cls, full: int, selected: int) -> ContextStats:
        selected = min(selected, full) if full else selected
        reduction = round(100 * (full - selected) / full, 1) if full else 0.0
        return cls(full, selected, max(0.0, reduction))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskContext:
    task: str  # ranking | tailoring | review | followup
    candidate: dict[str, Any]
    resume: dict[str, Any]
    relevant_experience: list[dict[str, Any]] = field(default_factory=list)
    relevant_skills: list[str] = field(default_factory=list)
    relevant_evidence: list[dict[str, Any]] = field(default_factory=list)
    unverified_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_facts: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    # Cover letters only: the candidate's own verified answers (notice period, work authorization, relocation).
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    certifications: list[dict[str, Any]] = field(default_factory=list)
    job: dict[str, Any] = field(default_factory=dict)
    fit_analysis: dict[str, Any] | None = None
    policy: dict[str, bool] = field(default_factory=dict)
    # Tailoring only: roles of the master resume as rendered, with the verified bullets tailoring may use.
    roles: list[dict[str, Any]] = field(default_factory=list)
    resume_text: str = ""  # a readable rendering of the selected facts (or the raw text when nothing is structured)
    stats: ContextStats = field(default_factory=ContextStats)

    @property
    def evidence_ids(self) -> set[int]:
        return {item["id"] for item in self.relevant_evidence}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def selected_payload(self) -> dict[str, Any]:
        """Everything the prompt receives from this context except the job description, which is measured apart."""
        out = self.to_dict()
        out.pop("stats")
        out["job"] = {k: v for k, v in out["job"].items() if k != "description"}
        return out


# ───────────── relevance ─────────────

def tokens(text: str) -> set[str]:
    from app.services.ranker import _tokens

    return _tokens(text)


@dataclass
class JobTerms:
    words: set[str]
    themes: set[str]
    text: str

    def score(self, text: str) -> int:
        found = tokens(text)
        theme_hits = sum(1 for theme in self.themes if found & set(THEMES[theme]))
        return len(found & self.words) + 2 * theme_hits


def job_terms(job: Job) -> JobTerms:
    text = " ".join([job.title or "", job.description or "", " ".join(job.skills or [])])
    words = tokens(text)
    lowered = set(re.findall(r"[a-z0-9][a-z0-9+#.-]*", text.lower()))
    themes = {name for name, vocab in THEMES.items() if len(lowered & set(vocab)) >= 2}
    return JobTerms(words, themes, text)


def _status(node: dict[str, Any]) -> str:
    return (node.get("verification") or {}).get("status") or "UNVERIFIED"


def _dates(role: dict[str, Any]) -> str:
    start = role.get("start_date") or role.get("raw_start_date") or ""
    end = "Present" if role.get("is_current") else role.get("end_date") or role.get("raw_end_date") or ""
    return f"{start} - {end}".strip(" -")


def _select_experience(data: dict[str, Any], terms: JobTerms, allowed: tuple[str, ...]
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(roles with their selected trusted bullets, selected unverified bullets). Headers of every non-rejected
    role are kept; a role header whose facts are unverified is marked."""
    roles, unverified = [], []
    for index, role in enumerate(data.get("experience") or []):
        role_status = _status(role)
        if role_status == "REJECTED":
            continue
        cap = BULLETS_RECENT_ROLE if index == 0 else BULLETS_PER_ROLE
        candidates = []
        for kind in ("achievements", "responsibilities"):
            for bullet in role.get(kind) or []:
                status = _status(bullet)
                if status == "REJECTED" or status not in allowed:
                    continue
                trusted = status == "VERIFIED" and role_status == "VERIFIED"
                score = terms.score(bullet["text"]) + (1 if kind == "achievements" else 0)
                candidates.append((score, trusted, bullet, kind))
        candidates.sort(key=lambda c: -c[0])
        chosen = [c for c in candidates if c[0] > 0][:cap] or candidates[:1 if index else 2]
        order = {id(c[2]): i for i, c in enumerate(candidates)}
        chosen.sort(key=lambda c: order[id(c[2])])
        bullets = []
        for _, trusted, bullet, kind in chosen:
            if trusted:
                bullets.append({"id": bullet["id"], "text": bullet["text"], "type": kind.removesuffix("s")})
            else:
                unverified.append({"id": bullet["id"], "text": bullet["text"], "role": role.get("job_title") or "",
                                   "company": role.get("company") or "", "status": _status(bullet)})
        if role_status != "VERIFIED" and role_status not in allowed:
            continue
        roles.append({"id": role["id"], "job_title": role.get("job_title") or "", "company": role.get("company") or "",
                      "location": role.get("location") or "", "dates": _dates(role),
                      "technologies": sr._mentions(terms.text, role.get("technologies") or []) or
                      (role.get("technologies") or [])[:8],
                      "verified": role_status == "VERIFIED", "bullets": bullets, "relevance": sum(c[0] for c in chosen)})
    return roles, unverified


def _select_skills(data: dict[str, Any], terms: JobTerms, allowed: tuple[str, ...]) -> list[str]:
    names = [s["name"] for s in data.get("skills") or [] if _status(s) in allowed]
    matched = sr._mentions(terms.text, names)
    return matched + [n for n in names if n not in matched][:EXTRA_SKILLS]


def select_evidence(db: Session, user_id: int, terms: JobTerms, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(relevant verified evidence, all verified evidence). Evidence never comes from rejected or unverified data."""
    rows = db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.verified.is_(True))
                      .order_by(Evidence.id)).all()
    every = [{"id": e.id, "company": e.company, "role": e.role, "claim": e.claim, "context": e.context,
              "skills": e.skills or [], "tags": e.tags or []} for e in rows]
    scored = [(terms.score(" ".join([e["claim"], e["context"], e["role"], " ".join(e["skills"]), " ".join(e["tags"])])), e)
              for e in every]
    chosen = [e for score, e in sorted(scored, key=lambda p: (-p[0], p[1]["id"])) if score > 0][:max(0, limit)]
    return sorted(chosen, key=lambda e: e["id"]), every


def _reviewed_list(data: dict[str, Any], section: str, allowed: tuple[str, ...], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{f: item.get(f) for f in fields if item.get(f)} | {"verified": _status(item) == "VERIFIED"}
            for item in data.get(section) or [] if _status(item) in allowed]


def _filtered(data: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """A copy holding only units with an allowed status, for derived facts that must not use other data."""
    out = {k: v for k, v in data.items() if k not in sr.LIST_SECTIONS}
    for section in sr.LIST_SECTIONS:
        items = []
        for item in data.get(section) or []:
            if _status(item) not in allowed:
                continue
            item = dict(item)
            for child in ("achievements", "responsibilities"):
                if child in item:
                    item[child] = [b for b in item[child] if _status(b) in allowed]
            items.append(item)
        out[section] = items
    return out


def _candidate(profile: CandidateProfile, data: dict[str, Any], allowed: tuple[str, ...]) -> tuple[dict[str, Any], list]:
    facts = profile_facts(profile, include_name=True)
    extra_unverified = []
    summary = data.get("summary") or {}
    if summary.get("text"):
        if _status(summary) == "VERIFIED":
            facts["summary"] = summary["text"]
        elif _status(summary) in allowed:
            extra_unverified.append({"id": summary.get("id", "summary"), "text": summary["text"], "role": "summary",
                                     "company": "", "status": _status(summary)})
    derived = sr.derive(_filtered(data, VERIFIED_ONLY))
    facts["verified_background"] = {k: v for k, v in derived.items() if v not in (None, [], False)}
    return facts, extra_unverified


def _base(db: Session, profile: CandidateProfile, job: Job, task: str, allowed: tuple[str, ...],
          evidence_limit: int) -> tuple[TaskContext, dict[str, Any], list[dict[str, Any]], Resume | None]:
    resume = rv.active_resume(db, profile)
    data = resume.structured_data if resume is not None else {}
    terms = job_terms(job)
    policy = candidate_policy(db, profile.user_id)
    experience, unverified = _select_experience(data or {}, terms, allowed)
    candidate, unverified_summary = _candidate(profile, data or {}, allowed)
    relevant, every = select_evidence(db, profile.user_id, terms, evidence_limit)
    education_fields = ("degree", "field_of_study", "institution", "end_date", "raw_end_date")
    context = TaskContext(
        task=task, candidate=candidate,
        resume={"id": resume.id, "version": resume.version, "verification_status": resume.verification_status}
        if resume is not None else {},
        relevant_experience=experience, relevant_skills=_select_skills(data or {}, terms, allowed),
        relevant_evidence=relevant, unverified_facts=unverified_summary + unverified,
        education=_reviewed_list(data or {}, "education", allowed, education_fields),
        certifications=_reviewed_list(data or {}, "certifications", allowed, ("name", "issuer", "issue_date")),
        job={**job_facts(job), "id": job.id}, policy=policy)
    if allowed == VERIFIED_ONLY:
        context.unverified_facts = []  # includes verified bullets of unverified roles: not trusted either
    return context, data or {}, every, resume


def _render(context: TaskContext) -> str:
    """The selected facts as short readable text (used as the resume section of a prompt)."""
    lines = []
    if context.candidate.get("summary"):
        lines += ["Summary", context.candidate["summary"], ""]
    if context.relevant_skills:
        lines += ["Skills", ", ".join(context.relevant_skills), ""]
    for role in context.relevant_experience:
        lines.append(" | ".join(filter(None, [role["job_title"], role["company"], role["location"], role["dates"]])))
        lines += [f"- {b['text']}" for b in role["bullets"]]
        lines.append("")
    if context.education:
        lines += ["Education", *[" | ".join(str(v) for k, v in e.items() if k != "verified") for e in context.education]]
    return "\n".join(lines).strip()


def _measure(context: TaskContext, raw_text: str, every_evidence: list[dict[str, Any]], job: Job) -> None:
    job_tokens = estimate_tokens(job.description or "")
    full = (estimate_tokens(json.dumps(context.candidate, default=str)) + estimate_tokens(raw_text)
            + estimate_tokens(json.dumps(every_evidence, default=str)) + job_tokens
            + estimate_tokens(json.dumps(context.fit_analysis or {}, default=str)))
    selected = estimate_tokens(json.dumps(context.selected_payload(), default=str)) + job_tokens
    context.stats = ContextStats.measure(max(full, selected), selected)


def has_verified_facts(data: dict[str, Any] | None, evidence: list[dict[str, Any]]) -> bool:
    return bool(rv.verified_facts(data) or evidence)


# ───────────── builders ─────────────

def build_ranking_context(db: Session, profile: CandidateProfile, job: Job, evidence_limit: int = 40) -> TaskContext:
    """Candidate summary, target roles, relevant experience and skills, relevant verified evidence, the job.
    Unverified parsed facts are included, marked lower-confidence, when the policy allows it."""
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["ranking_uses_unverified"] else VERIFIED_ONLY
    context, _data, every, resume = _base(db, profile, job, "ranking", allowed, evidence_limit)
    raw = resume.raw_text if resume is not None else profile.master_resume or ""
    if not context.relevant_experience:
        context.resume_text = raw  # nothing structured to select from: the resume as written
    else:
        context.resume_text = _render(context)
    _measure(context, raw, every, job)
    return context


def build_tailoring_context(db: Session, profile: CandidateProfile, job: Job) -> TaskContext:
    """The job, its fit analysis and resume gaps, the master resume roles with their verified bullets, and the
    relevant verified evidence and skills. Raises UnverifiedResumeError when there are no verified facts."""
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, data, every, resume = _base(db, profile, job, "tailoring", allowed, TAILOR_EVIDENCE)
    if not policy["allow_unverified_resume_facts"] and not has_verified_facts(data, every):
        raise UnverifiedResumeError(
            "Tailoring uses verified facts only. Verify your resume in Settings → Resume Data (or allow unverified "
            "resume facts in your candidate policy) and try again.")
    context.fit_analysis = job.fit_analysis or {}
    if policy["allow_unverified_resume_facts"]:
        # Allowed by the candidate policy: unverified bullets are used, and stay marked as such.
        for fact in context.unverified_facts:
            role = next((r for r in context.relevant_experience
                         if r["job_title"] == fact["role"] and r["company"] == fact["company"]), None)
            if role is not None:
                role["bullets"].append({"id": fact["id"], "text": fact["text"], "type": "unverified"})
    context.roles = master_roles(profile, data, context.relevant_experience)
    context.resume_text = _render(context)
    _measure(context, resume.raw_text if resume is not None else profile.master_resume or "", every, job)
    return context


def build_review_context(db: Session, profile: CandidateProfile, job: Job) -> TaskContext:
    """Structured verified truth (roles, dates, bullets, education, certifications), rejected facts, relevant verified
    evidence and CandidatePolicy, so the reviewer can check generated text against them."""
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, data, every, resume = _base(db, profile, job, "review", allowed, REVIEW_EVIDENCE)
    for role in data.get("experience") or []:  # the reviewer checks every verified role's dates, not only relevant ones
        if _status(role) == "VERIFIED" and role["id"] not in {r["id"] for r in context.relevant_experience}:
            context.relevant_experience.append({"id": role["id"], "job_title": role.get("job_title") or "",
                                                "company": role.get("company") or "", "location": role.get("location") or "",
                                                "dates": _dates(role), "technologies": [], "verified": True, "bullets": []})
    context.rejected_facts = [{"id": u.item_id, "section": u.section, "text": u.node.get("text") or rv._label(u)}
                              for u in sr.iter_units(data) if u.status == "REJECTED"]
    context.resume_text = _render(context)
    _measure(context, resume.raw_text if resume is not None else profile.master_resume or "", every, job)
    return context


def select_knowledge(db: Session, user_id: int, job: Job, terms: JobTerms) -> list[dict[str, Any]]:
    """Verified, non-sensitive answers the posting actually raises or shares vocabulary with."""
    posting = f"{job.title or ''} {job.description or ''}".lower()
    raised = {word for word in KNOWLEDGE_TRIGGERS if word in posting}
    rows = db.scalars(select(KnowledgeAnswer).where(KnowledgeAnswer.user_id == user_id,
                                                    KnowledgeAnswer.verified.is_(True),
                                                    KnowledgeAnswer.sensitive.is_(False))
                      .order_by(desc(KnowledgeAnswer.use_count), KnowledgeAnswer.id)).all()
    scored = []
    for row in rows:
        text = f"{row.question} {row.answer}"
        if not (row.answer or "").strip():
            continue
        score = terms.score(text) + 3 * sum(word in text.lower() for word in raised)
        if score:
            scored.append((score, row))
    chosen = sorted(scored, key=lambda pair: (-pair[0], pair[1].id))[:LETTER_KNOWLEDGE]
    return [{"question": row.question, "answer": row.answer, "category": row.category} for _, row in chosen]


def build_cover_letter_context(db: Session, profile: CandidateProfile, job: Job) -> TaskContext:
    """What a letter needs: the posting and its company facts, the candidate's verified proof points relevant to
    it, and the verified answers the posting raises. Verified facts only, like every other final application text."""
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, data, every, resume = _base(db, profile, job, "cover_letter", allowed, LETTER_EVIDENCE)
    if not policy["allow_unverified_resume_facts"] and not has_verified_facts(data, every):
        raise UnverifiedResumeError(
            "A cover letter uses verified facts only. Verify your resume in Settings → Resume Data (or allow "
            "unverified resume facts in your candidate policy) and try again.")
    personal = (data or {}).get("personal_information") or {}
    context.candidate = {**context.candidate,
                         # The letter signs off with a name: the profile's, or the one on the resume itself.
                         "name": context.candidate.get("name") or personal.get("full_name") or "",
                         "location": profile.location or personal.get("location") or "",
                         "work_authorization": profile.work_authorization or ""}
    context.job = {**context.job, "description": job.description or "",
                   **{field: getattr(job, field) for field in ("remote_type", "job_type", "seniority", "experience")
                      if getattr(job, field, None)}}
    context.knowledge = select_knowledge(db, profile.user_id, job, job_terms(job))
    context.fit_analysis = job.fit_analysis or {}
    context.resume_text = _render(context)
    _measure(context, resume.raw_text if resume is not None else profile.master_resume or "", every, job)
    return context


def build_followup_context(db: Session, profile: CandidateProfile, job: Job) -> TaskContext:
    """A follow-up only needs a few verified highlights relevant to the role."""
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, _data, every, resume = _base(db, profile, job, "followup", allowed, 5)
    context.relevant_experience = context.relevant_experience[:2]
    context.unverified_facts = context.unverified_facts if policy["allow_unverified_resume_facts"] else []
    context.resume_text = _render(context)
    _measure(context, resume.raw_text if resume is not None else profile.master_resume or "", every, job)
    return context


def master_roles(profile: CandidateProfile, data: dict[str, Any], selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map selected structured roles onto the roles of the master resume as tailoring renders it (role_index)."""
    from app.services.workflow import _best_match

    parsed = parse_resume(profile.master_resume or "")  # the same parse tailoring renders from
    headers = [role.header for role in parsed.roles]
    by_id = {role["id"]: role for role in data.get("experience") or []}
    out, used = [], set()
    for role in selected:
        entry = by_id.get(role["id"], {})
        index = entry.get("source_role_index")
        label = f"{role['job_title']} {role['company']}"
        if not (isinstance(index, int) and 0 <= index < len(headers) and index not in used
                and _best_match(label, [headers[index]])[1] >= 0.5):
            match, score = _best_match(label, [h for i, h in enumerate(headers) if i not in used])
            index = headers.index(match) if score >= 0.5 else None
        if index is None:
            continue
        used.add(index)
        out.append({"role_index": index, "title": parsed.roles[index].title, "company": parsed.roles[index].company,
                    "dates": parsed.roles[index].dates, "bullets": [b["text"] for b in role["bullets"]],
                    "verified": role["verified"]})
    return sorted(out, key=lambda r: r["role_index"])


def latest_ranking_context_hash(context: TaskContext) -> dict[str, Any]:
    """The parts of a ranking context that should trigger a fresh ranking when they change."""
    return {"experience": context.relevant_experience, "skills": context.relevant_skills,
            "unverified": context.unverified_facts, "candidate": context.candidate.get("summary"),
            "resume": context.resume.get("verification_status"), "evidence": context.relevant_evidence}

