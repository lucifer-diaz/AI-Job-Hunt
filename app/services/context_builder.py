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
* With `allow_unverified_resume_facts` on, an unverified fact of a resume read with extraction V2 reaches generation
  and review only when its confidence is high (app.services.resume_extraction.confidence); the others are held back
  (`held_back_facts`, with their level and reasons; a held role holds its bullets too). Facts a person edited are
  theirs and kept. A version read without V2 has no confidence and is used exactly as before. Ranking is not gated.

* The Tailor needs the Ranker's completed FitAnalysis. Without one the context is refused (RANKING_REQUIRED) and no
  model is called: an empty analysis is never sent for the Tailor to fill in.
* Generation workers leave out evidence whose provenance disagrees with its source resume item
  (app.services.evidence_provenance); it is flagged for review, never corrected here.
* Search and scraper configuration never enters a context (app.prompts.common.candidate_preferences).

Each context is checked before its prompt is built (`validate_tailoring_context`): inconsistent input fails here with
a ContextError code rather than being left for a prompt to compensate for.

Token awareness: every context records `full_available_context_tokens` (the whole resume text, all verified
evidence and the posting) against `selected_context_tokens` (what this context holds), and the reduction.
The numbers, and CONTEXT_BUILDER_VERSION, travel with the LLM call into LLMRequestLog (app.services.llm_telemetry).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    CandidateProfile,
    Evidence,
    Job,
    JobRanking,
    KnowledgeAnswer,
    Resume,
)
from app.prompts.common import candidate_preferences, job_facts, profile_facts
from app.services import job_context as jc
from app.services import resume_verification as rv
from app.services import structured_resume as sr
from app.services.job_description import normalize_description
from app.services.llm_telemetry import estimate_tokens
from app.services.resume_format import parse_resume
from app.services.user_settings import candidate_policy

log = logging.getLogger("jobhunt.context_builder")

# v5: the verified background (leadership, AI, cloud, data flags) is derived from verified units only, the summary
# included: an unverified or edited summary no longer sets a flag. Only that block changes; the ranking identity
# (latest_ranking_context_hash) does not cover it, so no job is re-ranked because of it.
# v4: generation tasks (tailoring, cover letters) pick bullets and evidence by the parsed posting's skills,
# responsibilities and keywords as well as its words; tailoring and letters also carry a requirement map (the Ranker's
# matched requirements with the ids that prove them) and keyword targets (app.services.ats_keywords.targets).
# v3: one copy of each fact. The resume (experience and projects) is the single source: each bullet carries one id,
# the evidence row created from it, and evidence created from the resume is never listed again as evidence. The
# evidence list holds only verified facts that are not on the resume; copies of resume bullets drop out, and
# near-identical items collapse to one. Evidence is sent as {id, company, role, claim}.
# v2: task-specific projections (no search configuration), the Tailor's canonical source resume with evidence ids per
# bullet, the completed-FitAnalysis gate, provenance exclusion and pre-prompt consistency checks.
CONTEXT_BUILDER_VERSION = "v5"
# Two texts state the same fact when this share of their distinctive words overlaps (Jaccard).
SAME_CLAIM = 0.6
BULLET_KINDS = {"achievements": "achievement", "responsibilities": "responsibility"}
MAX_PROJECTS = 3
BULLETS_PER_PROJECT = 3
VERIFIED_ONLY = ("VERIFIED",)
WITH_UNVERIFIED = ("VERIFIED", "UNVERIFIED", "EDITED")
HELD_BACK = "HELD_BACK"  # a context-only status: an unverified fact read with too little confidence to be used
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
# Tailoring: candidates sent per role beyond its bullet budget (app.services.resume_length), so the Tailor chooses.
CANDIDATE_SLACK = 3
EXTRA_SKILLS = 10
TAILOR_EVIDENCE = 20
REVIEW_EVIDENCE = 30
LETTER_EVIDENCE = 12
LETTER_KNOWLEDGE = 6
# Requirement map: the Ranker's matched requirements the Tailor and the letter are pointed at, REQUIRED first.
REQUIREMENT_MAP_LIMIT = 15
LETTER_REQUIREMENTS = 6
# Answers a letter may raise only because the posting does: the words that show the posting raised them.
KNOWLEDGE_TRIGGERS = ("visa", "sponsor", "sponsorship", "authorization", "authorisation", "work permit", "relocate",
                      "relocation", "notice", "start date", "availability", "onsite", "on-site", "hybrid", "remote")


class ContextError(ValueError):
    """A task's input is missing or inconsistent. Raised before any prompt is built, so no model is called.

    Codes: RANKING_REQUIRED, FIT_ANALYSIS_MISMATCH, TAILOR_CONTEXT_INCOMPLETE, TAILOR_CONTEXT_INVALID,
    REVIEW_CONTEXT_INCOMPLETE."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class UnverifiedResumeError(ValueError):
    """Final application content needs verified facts and the candidate has none."""


@dataclass
class ContextStats:
    full_available_context_tokens: int = 0
    selected_context_tokens: int = 0
    context_reduction_percent: float = 0.0
    # Set only when the worker has a prompt token budget (fit_prompt): the budget, the prompt's estimated tokens
    # before and after trimming, and how many items of each kind were dropped to fit it.
    prompt_token_budget: int | None = None
    prompt_tokens_before_trim: int | None = None
    prompt_tokens: int | None = None
    trimmed: dict[str, int] = field(default_factory=dict)

    @classmethod
    def measure(cls, full: int, selected: int) -> ContextStats:
        selected = min(selected, full) if full else selected
        reduction = round(100 * (full - selected) / full, 1) if full else 0.0
        return cls(full, selected, max(0.0, reduction))

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.prompt_token_budget is None:  # no budget: the stats are exactly what they were before budgets
            for key in ("prompt_token_budget", "prompt_tokens_before_trim", "prompt_tokens", "trimmed"):
                out.pop(key)
        return out


@dataclass
class TaskContext:
    task: str  # ranking | tailoring | review | followup
    candidate: dict[str, Any]
    resume: dict[str, Any]
    relevant_experience: list[dict[str, Any]] = field(default_factory=list)
    relevant_projects: list[dict[str, Any]] = field(default_factory=list)
    relevant_skills: list[str] = field(default_factory=list)
    relevant_evidence: list[dict[str, Any]] = field(default_factory=list)
    unverified_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_facts: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    # Cover letters only: the candidate's own verified answers (notice period, work authorization, relocation).
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    certifications: list[dict[str, Any]] = field(default_factory=list)
    job: dict[str, Any] = field(default_factory=dict)
    # The posting as structured requirements (app.services.job_context), not the whole advert.
    job_context: dict[str, Any] = field(default_factory=dict)
    fit_analysis: dict[str, Any] | None = None
    policy: dict[str, bool] = field(default_factory=dict)
    # Tailoring only: roles of the master resume as rendered, with the verified bullets tailoring may use.
    roles: list[dict[str, Any]] = field(default_factory=list)
    # Tailoring only: the page target and each role's bullet budget (app.services.resume_length), as the prompt
    # states it: {"pages", "budgets", "role_caps": {role_index: max bullets}}.
    length_plan: dict[str, Any] = field(default_factory=dict)
    # Tailoring only: the one canonical copy of the source resume the prompt receives (summary, skills, roles with
    # bullets and the evidence ids behind each).
    source_resume: dict[str, Any] = field(default_factory=dict)
    # Tailoring and letters: the Ranker's MATCH / PARTIAL_MATCH requirements with the citable ids that prove them.
    requirement_map: list[dict[str, Any]] = field(default_factory=list)
    # Tailoring and letters: the posting's ATS keywords split into those the facts support ("use") and gaps.
    keyword_targets: dict[str, Any] = field(default_factory=dict)
    # Cover letters only: the validated tailored resume the letter accompanies (summary, top bullets).
    tailored: dict[str, Any] = field(default_factory=dict)
    # Tailoring revision passes only: the previous version's review (review_results.retailor_feedback).
    revision_feedback: dict[str, Any] = field(default_factory=dict)
    # Evidence held back from generation because its provenance disagrees with its source ({id, code}).
    excluded_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Unverified resume facts held back because their reading is not high confidence ({id, section, text, level,
    # reasons}); the reviewer sees them to flag a claim that relies on one.
    held_back_facts: list[dict[str, Any]] = field(default_factory=list)
    resume_text: str = ""  # a readable rendering of the selected facts (or the raw text when nothing is structured)
    stats: ContextStats = field(default_factory=ContextStats)
    # Not a field, so never part of the prompt payload (to_dict): {retrieval_ms, retrieval_mode} for call_stats, set
    # by _base when retrieval ran for this context.
    retrieval = MappingProxyType({})
    job_stats: jc.JobContextStats = field(default_factory=jc.JobContextStats)

    @property
    def evidence_ids(self) -> set[int]:
        """Every evidence id this context lets a model cite: the evidence list and the ids on verified bullets."""
        return {item["id"] for item in self.relevant_evidence} | {
            i for unit in [*self.relevant_experience, *self.relevant_projects] for bullet in unit.get("bullets") or []
            for i in bullet.get("evidence_ids") or []}

    @property
    def citable_ids(self) -> set[int]:
        """What the Tailor may cite: the evidence list and the id of every source bullet and project bullet."""
        return {item["id"] for item in self.relevant_evidence} | {
            bullet["id"] for part in ("roles", "projects") for unit in self.source_resume.get(part) or []
            for bullet in unit.get("bullets") or [] if "id" in bullet}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def call_stats(self) -> dict[str, Any]:
        """Both sides of context selection, and the evidence retrieval behind it, for the LLM request log."""
        return {**self.stats.as_dict(), **self.job_stats.as_dict(), **self.retrieval,
                "context_builder_version": CONTEXT_BUILDER_VERSION}

    def selected_payload(self) -> dict[str, Any]:
        """Everything the prompt receives from this context except the job description, which is measured apart."""
        out = self.to_dict()
        out.pop("stats")
        out.pop("job_stats", None)
        out.pop("job_context", None)  # measured by app.services.job_context, not counted twice here
        out.pop("excluded_evidence", None)
        out.pop("held_back_facts", None)
        out["job"] = {k: v for k, v in out["job"].items() if k != "description"}
        if out.get("source_resume"):  # the Tailor's canonical copy replaces the selections it was built from
            for duplicate in ("roles", "relevant_experience", "resume_text", "relevant_skills"):
                out.pop(duplicate, None)
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
    # Words of what the parsed posting asks for (skills, responsibilities, keywords): they count twice.
    focus: set[str] = field(default_factory=set)

    def score(self, text: str) -> int:
        found = tokens(text)
        theme_hits = sum(1 for theme in self.themes if found & set(THEMES[theme]))
        return len(found & self.words) + len(found & self.focus) + 2 * theme_hits


def job_terms(job: Job) -> JobTerms:
    text = " ".join([job.title or "", job.description or "", " ".join(job.skills or [])])
    words = tokens(text)
    lowered = set(re.findall(r"[a-z0-9][a-z0-9+#.-]*", text.lower()))
    themes = {name for name, vocab in THEMES.items() if len(lowered & set(vocab)) >= 2}
    return JobTerms(words, themes, text)


def generation_terms(job: Job) -> JobTerms:
    """job_terms, with the parsed posting's required and preferred skills, responsibilities and keywords weighted,
    so a bullet that answers what the employer asks outranks one that shares words with the advert's boilerplate.
    Generation tasks only: ranking keeps job_terms, so its context (and its hashes) do not move."""
    terms = job_terms(job)
    data = jc._structured(job)
    focus = " ".join(str(v) for key in ("required_skills", "preferred_skills", "keywords", "responsibilities",
                                        "technical_domains") for v in data.get(key) or [])
    terms.focus = tokens(focus) - jc.FILLER
    terms.text = f"{terms.text} {focus}"
    return terms


def _status(node: dict[str, Any]) -> str:
    return (node.get("verification") or {}).get("status") or "UNVERIFIED"


def _dates(role: dict[str, Any]) -> str:
    start = role.get("start_date") or role.get("raw_start_date") or ""
    end = "Present" if role.get("is_current") else role.get("end_date") or role.get("raw_end_date") or ""
    return f"{start} - {end}".strip(" -")


def _select_experience(data: dict[str, Any], terms: JobTerms, allowed: tuple[str, ...],
                       caps: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(roles with their selected trusted bullets, selected unverified bullets). Headers of every non-rejected
    role are kept; a role header whose facts are unverified is marked.

    `caps` (tailoring) is each role's bullet budget by role id: up to budget + CANDIDATE_SLACK candidates are sent,
    most relevant first, and a role whose budget is 0 keeps its heading with no bullets."""
    roles, unverified = [], []
    for index, role in enumerate(data.get("experience") or []):
        role_status = _status(role)
        if role_status == "REJECTED":
            continue
        budget = caps.get(role.get("id")) if caps is not None else None
        cap = budget + CANDIDATE_SLACK if budget else 0 if budget == 0 else (
            BULLETS_RECENT_ROLE if index == 0 else BULLETS_PER_ROLE)
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
        chosen = [] if budget == 0 else [c for c in candidates if c[0] > 0][:cap] or candidates[:1 if index else 2]
        if budget:  # a budgeted role offers its full budget, relevant bullets first, so the Tailor has a choice
            chosen = (chosen + [c for c in candidates if c not in chosen])[:cap]
        order = {id(c[2]): i for i, c in enumerate(candidates)}
        chosen.sort(key=lambda c: order[id(c[2])])
        bullets = []
        for _, trusted, bullet, kind in chosen:
            if trusted:
                bullets.append({"id": bullet["id"], "text": bullet["text"], "type": BULLET_KINDS[kind]})
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


def _select_projects(data: dict[str, Any], terms: JobTerms, allowed: tuple[str, ...]) -> list[dict[str, Any]]:
    """Resume projects related to the posting, with their related verified bullets. Projects are resume facts like
    roles; they used to reach prompts only as evidence rows."""
    scored = []
    for project in data.get("projects") or []:
        if _status(project) == "REJECTED" or _status(project) not in allowed:
            continue
        bullets = []
        for kind in ("achievements", "responsibilities"):
            for bullet in project.get(kind) or []:
                verified = _status(bullet) == "VERIFIED" and _status(project) == "VERIFIED"
                if verified or (allowed != VERIFIED_ONLY and _status(bullet) in allowed):
                    bullets.append((terms.score(bullet["text"]), bullet))
        header = " ".join([project.get("name") or "", project.get("description") or "",
                           " ".join(project.get("technologies") or [])])
        score = terms.score(header) + sum(points for points, _ in bullets)
        chosen = [b for points, b in sorted(bullets, key=lambda pair: -pair[0]) if points > 0][:BULLETS_PER_PROJECT]
        if score > 0 and (chosen or bullets):
            scored.append((score, {"id": project.get("id"), "name": project.get("name") or "",
                                   "technologies": project.get("technologies") or [],
                                   "bullets": [{"id": b["id"], "text": b["text"]}
                                               for b in (chosen or [bullets[0][1]])]}))
    return [project for _, project in sorted(scored, key=lambda pair: -pair[0])[:MAX_PROJECTS]]


def _select_skills(data: dict[str, Any], terms: JobTerms, allowed: tuple[str, ...]) -> list[str]:
    names = [s["name"] for s in data.get("skills") or [] if _status(s) in allowed]
    matched = sr._mentions(terms.text, names)
    return matched + [n for n in names if n not in matched][:EXTRA_SKILLS]


def select_evidence(db: Session, user_id: int, terms: JobTerms, limit: int,
                    exclude: set[int] | frozenset[int] = frozenset(), resume_items: set[str] | frozenset[str] = frozenset()
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(relevant verified evidence, all verified evidence). Evidence never comes from rejected or unverified data;
    `exclude` holds ids generation must not use (provenance conflicts). Evidence created from one of `resume_items`
    is the resume itself: the context carries it as that item's bullet, so it is not selected again here."""
    rows = [e for e in db.scalars(select(Evidence).where(Evidence.user_id == user_id, Evidence.verified.is_(True))
                                  .order_by(Evidence.id)).all()
            if e.id not in exclude and not (e.source == "resume" and e.structured_item_id in resume_items)]
    every = [{"id": e.id, "company": e.company, "role": e.role, "claim": e.claim, "context": e.context,
              "skills": e.skills or [], "tags": e.tags or []} for e in rows]
    scored = [(terms.score(" ".join([e["claim"], e["context"], e["role"], " ".join(e["skills"]), " ".join(e["tags"])])), e)
              for e in every]
    chosen = [e for score, e in sorted(scored, key=lambda p: (-p[0], p[1]["id"])) if score > 0][:max(0, limit)]
    # What a prompt receives: the claim and where it happened. Skills and tags were only used for relevance above
    # (and are often attached loosely), the context is provenance text such as "Verified from resume v2".
    projected = [{k: e[k] for k in ("id", "company", "role", "claim") if e[k]} | {"id": e["id"]} for e in chosen]
    return sorted(projected, key=lambda e: e["id"]), every


def retrieved_evidence(db: Session, profile: Any, job: Job, task: str, terms: JobTerms, limit: int,
                       deterministic: list[dict[str, Any]], held_back: set[int], resume_items: set[str],
                       query: str | None = None, timing: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The evidence a job context carries, by RAG_RETRIEVAL_MODE:

        off      `deterministic` (select_evidence), exactly as before — the default and the rollback
        shadow   `deterministic`; retrieval runs beside it and the difference (ids only) goes to Monitoring → Logs
        on       retrieval (app.services.rag_retrieval): only the job's own resume's evidence and untagged evidence,
                 relevance-ranked, within the token budget; on any retrieval error, `deterministic`

    Retrieval honours the same exclusions: evidence held back for a provenance conflict, and evidence created from a
    bullet the context already carries as that bullet.

    `timing`, when given, receives what the call's telemetry records (llm_request_logs.retrieval_ms / retrieval_mode):
    the retrieval's own measured latency, and "on" or "fallback". Left empty when no retrieval was used."""
    from app.settings import get_settings

    mode = get_settings().rag_mode
    if mode == "off" or getattr(job, "id", None) is None:
        return deterministic
    from app.services import rag_retrieval, system_log

    shown = set(db.scalars(select(Evidence.id).where(
        Evidence.user_id == profile.user_id, Evidence.source == "resume",
        Evidence.structured_item_id.in_(sorted(resume_items)))).all()) if resume_items else set()
    try:
        pack = rag_retrieval.retrieve_evidence(db, profile, job, terms=terms, exclude=held_back | shown,
                                               limit=limit, query=query)
    except Exception as exc:  # noqa: BLE001  the deterministic builder is the fallback
        system_log.log_event(message=f"Retrieval failed for job {job.id}; the deterministic context was used",
                             category="WORKFLOW", level="WARNING", operation="rag_retrieval", user_id=profile.user_id,
                             entity_type="job", entity_id=job.id, status="FALLBACK",
                             details={"task": task, "error": type(exc).__name__})
        if timing is not None:
            timing["retrieval_mode"] = "fallback"
        return deterministic
    if mode == "shadow":
        old, new = {e["id"] for e in deterministic}, set(pack.evidence_ids)
        system_log.log_event(
            message=f"Retrieval shadow comparison for job {job.id} ({task})", category="WORKFLOW",
            operation="rag_shadow", user_id=profile.user_id, entity_type="job", entity_id=job.id,
            status="SAME" if old == new else "DIFFERENT", duration_ms=pack.latency_ms,
            details={"task": task, "only_deterministic": sorted(old - new), "only_retrieval": sorted(new - old),
                     "outside_resume_scope": sorted(old - pack_scope(db, profile, job)), **pack.metadata()})
        return deterministic
    if timing is not None:
        timing.update(retrieval_ms=pack.latency_ms, retrieval_mode="on")
    return pack.projected()


def scoped_evidence(db: Session, profile: Any, job: Job, every: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The verified evidence a job's gates and measurements count (`has_verified_facts`, `_measure`). With
    RAG_RETRIEVAL_MODE=on, only what the job's own resume may use, so another resume's facts can never satisfy the
    verified-facts gate; otherwise, or if the scope cannot be read, `every` unchanged (as retrieval falls back)."""
    from app.settings import get_settings

    if get_settings().rag_mode != "on" or getattr(job, "id", None) is None:
        return every
    try:
        scope = pack_scope(db, profile, job)
    except Exception:  # like retrieval: the deterministic, account-wide view is the fallback
        log.warning("resume scope for job %s could not be read; gates use all verified evidence", job.id,
                    exc_info=True)
        return every
    return [e for e in every if e["id"] in scope]


def pack_scope(db: Session, profile: Any, job: Job) -> set[int]:
    from app.services import rag_retrieval

    return rag_retrieval.scope(db, profile, job)[1]


def same_claim(a: str, b: str) -> bool:
    """Whether two texts state the same fact in different words (typos and small rewordings included)."""
    left, right = tokens(a), tokens(b)
    return bool(left and right) and len(left & right) / len(left | right) >= SAME_CLAIM


def consolidate(db: Session, user_id: int, units: list[dict[str, Any]], evidence: list[dict[str, Any]],
                exclude: set[int], resume_texts: list[str] | None = None) -> list[dict[str, Any]]:
    """One copy of each fact. Each verified bullet (of a role or a project) gets the ids of the evidence behind it:
    first the evidence created from it (its citation id), then evidence at the same employer stating the same thing.
    Evidence a bullet states leaves the list, and evidence stating one fact twice collapses to the newest item.
    Evidence repeating any other bullet of the resume (`resume_texts`, including bullets not selected for this job)
    is resume data too and is dropped. Returns the evidence still to send: verified facts that are not on the resume."""
    from app.services.evidence_provenance import same_name

    item_evidence = _item_evidence(db, user_id, exclude)
    covered: set[int] = set()
    for unit in units:
        for bullet in unit.get("bullets") or []:
            own = item_evidence.get(bullet.get("id") or "", [])
            merged = []
            for item in evidence:
                if same_claim(item["claim"], bullet["text"]):
                    covered.add(item["id"])  # the bullet says it; a different employer is a provenance issue
                    if same_name(item.get("company"), unit.get("company")) and item["id"] not in own:
                        merged.append(item["id"])
            bullet["evidence_ids"] = [*own, *sorted(merged)]
            covered |= set(bullet["evidence_ids"])
    covered |= {item["id"] for item in evidence
                if any(same_claim(item["claim"], text) for text in resume_texts or [])}
    kept: list[dict[str, Any]] = []
    for item in sorted((e for e in evidence if e["id"] not in covered), key=lambda e: -e["id"]):
        if not any(same_claim(item["claim"], other["claim"]) for other in kept):
            kept.append(item)
    return sorted(kept, key=lambda e: e["id"])


def _reviewed_list(data: dict[str, Any], section: str, allowed: tuple[str, ...], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Education or certifications with the fields a prompt uses; "verified": false marks only what is not verified."""
    return [{f: item.get(f) for f in fields if item.get(f)} | ({} if _status(item) == "VERIFIED" else {"verified": False})
            for item in data.get(section) or [] if _status(item) in allowed]


def _filtered(data: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """A copy holding only units with an allowed status, for derived facts that must not use other data. The summary
    is a unit too: structured_resume.derive reads its text, so an unverified one must not reach it."""
    out = {k: v for k, v in data.items() if k not in sr.LIST_SECTIONS}
    summary = out.get("summary")
    if summary and (not isinstance(summary, dict) or _status(summary) not in allowed):
        out.pop("summary")
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
    facts["search_preferences"] = candidate_preferences(facts.get("search_preferences"))  # never search settings
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


GENERATION_TASKS = ("tailoring", "cover_letter", "followup", "application_answer", "application_form")


def hold_back_unconfident(db: Session, resume: Resume | None, data: dict[str, Any] | None, allowed: tuple[str, ...],
                          task: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the data with unverified facts below high confidence marked HELD_BACK, what was held back).

    Only when unverified facts are allowed at all, for generation and review (never ranking), and only for a version
    read with V2: without confidence the data is returned as it is. A fact a person verified, edited or rejected is
    theirs and never touched, and the stored data is never changed (the marks are on a copy)."""
    if not data or resume is None or "UNVERIFIED" not in allowed or task == "ranking":
        return data or {}, []
    levels = rv.confidence(db, resume)
    if not levels:
        return data, []
    data = copy.deepcopy(data)
    held = []
    for unit in sr.iter_units(data):
        if unit.kind == "personal" or unit.status != "UNVERIFIED":
            continue
        level = levels.get(unit.item_id)
        by_parent = unit.parent is not None and _status(unit.parent) == HELD_BACK
        if not by_parent and (level is None or level["level"] == "high"):
            continue
        unit.node["verification"] = {**(unit.node.get("verification") or {}), "status": HELD_BACK}
        held.append({"id": unit.item_id, "section": unit.section, "text": unit.node.get("text") or rv._label(unit),
                     "level": level["level"] if level else "unknown",
                     "reasons": (level or {}).get("reasons", [])[:3] + (["its role is held back"] if by_parent else [])})
    return data, held


def _base(db: Session, profile: CandidateProfile, job: Job, task: str, allowed: tuple[str, ...],
          evidence_limit: int, caps: dict[str, int] | None = None, terms: JobTerms | None = None,
          out_of_scope: set[int] | frozenset[int] = frozenset()
          ) -> tuple[TaskContext, dict[str, Any], list[dict[str, Any]], Resume | None]:
    from app.services.evidence_provenance import excluded_ids

    resume = rv.resume_for(db, profile)
    data = resume.structured_data if resume is not None else {}
    data, held_facts = hold_back_unconfident(db, resume, data, allowed, task)
    terms = terms or (generation_terms(job) if task in ("tailoring", "cover_letter") else job_terms(job))
    policy = candidate_policy(db, profile.user_id)
    experience, unverified = _select_experience(data or {}, terms, allowed, caps)
    candidate, unverified_summary = _candidate(profile, data or {}, allowed)
    held_back = excluded_ids(db, profile.user_id) if task in GENERATION_TASKS else {}
    resume_items = {unit.item_id for unit in sr.iter_units(data or {})}
    relevant, every = select_evidence(db, profile.user_id, terms, evidence_limit,
                                      exclude=set(held_back) | set(out_of_scope), resume_items=resume_items)
    retrieval: dict[str, Any] = {}
    relevant = retrieved_evidence(db, profile, job, task, terms, evidence_limit, relevant,
                                  set(held_back) | set(out_of_scope), resume_items, timing=retrieval)
    every = scoped_evidence(db, profile, job, every)
    projects = _select_projects(data or {}, terms, allowed)
    education_fields = ("degree", "field_of_study", "institution", "university", "grade", "end_date",
                        "raw_end_date")
    context = TaskContext(
        task=task, candidate=candidate,
        resume={"id": resume.id, "version": resume.version, "verification_status": resume.verification_status}
        if resume is not None else {},
        relevant_experience=experience, relevant_projects=projects,
        relevant_skills=_select_skills(data or {}, terms, allowed),
        relevant_evidence=relevant, unverified_facts=unverified_summary + unverified,
        education=_reviewed_list(data or {}, "education", allowed, education_fields),
        certifications=_reviewed_list(data or {}, "certifications", allowed, ("name", "issuer", "issue_date")),
        job={**job_facts(job), "id": getattr(job, "id", None)}, policy=policy,
        excluded_evidence=[{"id": i, "code": code} for i, code in sorted(held_back.items())],
        held_back_facts=held_facts)
    resume_texts = [unit.node.get("text") or "" for unit in sr.iter_units(data or {}) if unit.kind == "bullet"]
    context.relevant_evidence = consolidate(db, profile.user_id, [*context.relevant_experience, *projects],
                                            context.relevant_evidence, set(held_back), resume_texts)
    if allowed == VERIFIED_ONLY:
        context.unverified_facts = []  # includes verified bullets of unverified roles: not trusted either
    if retrieval:
        context.retrieval = retrieval
    return context, data or {}, every, resume


def _with_job(context: TaskContext, job: jc.JobContext) -> TaskContext:
    """Attach the posting's structured requirements. The raw description travels only when nothing was parsed."""
    context.job_context = job.to_dict()
    context.job_stats = job.stats
    if job.structured:
        context.job = {k: v for k, v in context.job.items() if k != "description"}
    return context


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
    for project in context.relevant_projects:
        lines.append(f"Project: {project['name']}")
        lines += [f"- {b['text']}" for b in project["bullets"]]
        lines.append("")
    if context.education:
        lines += ["Education", *[" | ".join(str(v) for k, v in e.items() if k != "verified") for e in context.education]]
    return "\n".join(lines).strip()


def _measure(context: TaskContext, raw_text: str, every_evidence: list[dict[str, Any]], job: Job) -> None:
    job_tokens = estimate_tokens(job.description or "")
    full = (estimate_tokens(json.dumps(context.candidate, default=str)) + estimate_tokens(raw_text)
            + estimate_tokens(json.dumps(every_evidence, default=str)) + job_tokens
            + estimate_tokens(json.dumps(context.fit_analysis or {}, default=str)))
    # The raw posting counts as selected only when it is actually sent; a parsed posting travels as structured
    # requirements, measured on the posting side (context.job_stats).
    sent_job = 0 if (context.job_context or {}).get("structured") else job_tokens
    selected = estimate_tokens(json.dumps(context.selected_payload(), default=str)) + sent_job
    context.stats = ContextStats.measure(max(full, selected), selected)


def has_verified_facts(data: dict[str, Any] | None, evidence: list[dict[str, Any]]) -> bool:
    return bool(rv.verified_facts(data) or evidence)


# ───────────── prompt token budget ─────────────

# What a context loses, in this order, when its prompt is over the worker's prompt token budget
# (app.services.prompt_budget). Each step drops one item at a time, the least relevant first, until the prompt fits.
# Never trimmed: REQUIRED requirements (job_context.required, hard_requirements and the employer's sentences for them,
# REQUIRED rows of the requirement map), the candidate's identity, summary and verified background, role headers,
# each role's most relevant bullet (ranking) or its budgeted bullets (tailoring), the fit analysis and the length plan.
RANKING_TRIM_ORDER = (
    "unverified_facts",        # lower-confidence facts, the last listed first
    "extra_skills",            # skills the posting does not mention, the last listed first
    "projects",                # resume projects, the least relevant first
    "preferred_requirements",  # PREFERRED requirements, the lowest parse confidence first, then the last listed
    "responsibilities",        # beyond the first TRIM_KEEP_RESPONSIBILITIES, the last listed first
    "evidence",                # verified evidence, the lowest relevance score first
    "bullets",                 # each role's last (least relevant) bullet, the lowest-scoring first; a role keeps one
)
TAILORING_TRIM_ORDER = (
    "spare_bullets",              # source bullets beyond a role's budget (its CANDIDATE_SLACK), each role's last first
    "projects",                   # read-only project context, the least relevant first
    "preferred_requirement_map",  # PREFERRED rows of the requirement map, the last listed first
    "preferred_requirements",
    "responsibilities",
    "evidence",
)
TRIM_KEEP_RESPONSIBILITIES = 3


def _drop_preferred(context: TaskContext) -> Any:
    from app.services.prompt_budget import drop_lowest

    job = context.job_context

    def forget(item: dict[str, Any]) -> None:  # the term overlap names requirement ids: never one no longer shown
        for key in ("candidate_matches", "candidate_gaps"):
            job[key] = [m for m in job.get(key) or [] if not (m.get("id") == item.get("id")
                                                              and m.get("level") != jc.REQUIRED)]

    items = job.get("preferred") if isinstance(job.get("preferred"), list) else []
    return drop_lowest(items, lambda r: (r.get("confidence") or 0,), removed=forget)


def _drop_extra_skill(context: TaskContext, terms: JobTerms) -> Any:
    def drop() -> bool:
        matched = set(sr._mentions(terms.text, context.relevant_skills))
        for index in range(len(context.relevant_skills) - 1, -1, -1):
            if context.relevant_skills[index] not in matched:
                del context.relevant_skills[index]
                return True
        return False
    return drop


def _drop_ranking_bullet(context: TaskContext, terms: JobTerms) -> Any:
    def drop() -> bool:
        # A role's bullets are already most relevant first (_select_experience): each role offers its last one, and
        # the least relevant of those goes (an older role first on a tie).
        spare = [(terms.score(role["bullets"][-1]["text"]), -r, r) for r, role in enumerate(context.relevant_experience)
                 if len(role.get("bullets") or []) > 1]
        if not spare:
            return False
        *_, r = min(spare)
        context.relevant_experience[r]["bullets"].pop()
        return True
    return drop


def _drop_spare_bullet(context: TaskContext, terms: JobTerms) -> Any:
    """A source bullet beyond its role's budget, from context.roles, the source resume and the selection behind it
    alike, so the citable ids (citable_ids, evidence_ids) are exactly the bullets the prompt still shows."""
    caps = (context.length_plan or {}).get("role_caps") or {}
    source = {role["role_index"]: role for role in context.source_resume.get("roles") or []}

    def drop() -> bool:
        # Each role over its budget offers its last bullet (they are most relevant first), the least relevant goes.
        spare = [(terms.score(role["bullets"][-1]), -r, r) for r, role in enumerate(context.roles)
                 if len(role["bullets"]) > caps.get(str(role["role_index"]), len(role["bullets"]))]
        if not spare:
            return False
        *_, r = min(spare)
        role = context.roles[r]
        i = len(role["bullets"]) - 1
        ids = role.get("bullet_ids") or []
        item_id = ids[i] if i < len(ids) else None
        for key in ("bullets", "bullet_ids", "bullet_evidence_ids"):
            if i < len(role.get(key) or []):
                del role[key][i]
        shown = source.get(role["role_index"])
        if shown is not None and i < len(shown.get("bullets") or []):
            del shown["bullets"][i]
        if item_id is not None:
            for unit in context.relevant_experience:
                unit["bullets"] = [b for b in unit.get("bullets") or [] if b.get("id") != item_id]
        return True
    return drop


def _drop_project(context: TaskContext) -> Any:
    def drop() -> bool:
        if not context.relevant_projects:
            return False
        shown = context.source_resume.get("projects")
        rest = any(v for k, v in context.source_resume.items() if k != "projects")
        if shown and len(shown) == 1 and not rest and not context.relevant_evidence:
            return False  # the only thing left to tailor from (validate_tailoring_context): it stays
        context.relevant_projects.pop()  # ordered most relevant first (_select_projects)
        if shown:
            shown.pop()
            if not shown:
                del context.source_resume["projects"]
        return True
    return drop


def _drop_preferred_map_row(context: TaskContext) -> Any:
    def drop() -> bool:
        for index in range(len(context.requirement_map) - 1, -1, -1):
            if context.requirement_map[index].get("level") != jc.REQUIRED:
                del context.requirement_map[index]
                return True
        return False
    return drop


def _drop_tailoring_evidence(context: TaskContext, score: Any) -> Any:
    """The Tailor needs something to tailor from: with no source resume left (projects may have gone before), the
    last evidence item stays. Decided at each drop, not when the steps are built."""
    from app.services.prompt_budget import drop_lowest

    lowest = drop_lowest(context.relevant_evidence, score)

    def drop() -> bool:
        if not context.source_resume and len(context.relevant_evidence) <= 1:
            return False
        return lowest()
    return drop


def _refresh_targets(context: TaskContext, job: Job) -> None:
    """After a tailoring fact left the prompt: the requirement map cites only ids still shown, and the keyword
    targets count as supported only what the remaining facts prove."""
    citable = context.citable_ids
    for row in context.requirement_map:
        ids = [i for i in row.get("evidence_ids") or [] if i in citable]
        if ids:
            row["evidence_ids"] = ids
        else:
            row.pop("evidence_ids", None)
    context.keyword_targets = keyword_targets(job, _shown_texts(context), _tailoring_sources(context))


def _rematching(context: TaskContext) -> Any:
    """Wraps a ranking drop of a candidate fact: the term overlap (candidate_matches / candidate_gaps) is counted
    again from the facts still shown, so it never names a term only a dropped fact had. Only when the overlap was
    counted from these facts in the first place (not with resume_source master_text, whose overlap comes from data
    the prompt does not show): otherwise it is left as the builder made it."""
    job = context.job_context or {}

    def requirements() -> list[dict[str, Any]]:
        return list(job.get("required") or []) + list(job.get("preferred") or [])

    current = bool(job.get("structured")) and (job.get("candidate_matches"), job.get("candidate_gaps")) == tuple(
        jc.candidate_match(requirements(), context))

    def wrap(drop: Any) -> Any:
        def run() -> bool:
            if not drop():
                return False
            if current:
                job["candidate_matches"], job["candidate_gaps"] = jc.candidate_match(requirements(), context)
            return True
        return run
    return wrap


def trim_steps(context: TaskContext, job: Job) -> list[Any]:
    """The trim steps of a ranking or tailoring context, in RANKING_TRIM_ORDER / TAILORING_TRIM_ORDER."""
    from app.services.prompt_budget import Step, drop_last, drop_lowest

    def evidence_score(e: dict[str, Any]) -> tuple:
        return (terms.score(" ".join(str(e.get(k) or "") for k in ("company", "role", "claim"))),)

    listed = (context.job_context or {}).get("responsibilities")
    responsibilities = listed if isinstance(listed, list) else []
    if context.task == "ranking":
        terms = job_terms(job)
        rematched = _rematching(context)  # skills, bullets and evidence are what the term overlap counts
        steps = {"unverified_facts": drop_last(context.unverified_facts),
                 "extra_skills": rematched(_drop_extra_skill(context, terms)),
                 "projects": drop_last(context.relevant_projects),
                 "preferred_requirements": _drop_preferred(context),
                 "responsibilities": drop_last(responsibilities, TRIM_KEEP_RESPONSIBILITIES),
                 "evidence": rematched(drop_lowest(context.relevant_evidence, evidence_score)),
                 "bullets": rematched(_drop_ranking_bullet(context, terms))}
        return [Step(kind, steps[kind]) for kind in RANKING_TRIM_ORDER]
    if context.task != "tailoring":
        return []
    terms = generation_terms(job)

    def refreshed(drop: Any) -> Any:
        def run() -> bool:
            if not drop():
                return False
            _refresh_targets(context, job)
            return True
        return run

    steps = {"spare_bullets": refreshed(_drop_spare_bullet(context, terms)),
             "projects": refreshed(_drop_project(context)),
             "preferred_requirement_map": _drop_preferred_map_row(context),
             "preferred_requirements": _drop_preferred(context),
             "responsibilities": drop_last(responsibilities, TRIM_KEEP_RESPONSIBILITIES),
             "evidence": refreshed(_drop_tailoring_evidence(context, evidence_score))}
    return [Step(kind, steps[kind]) for kind in TAILORING_TRIM_ORDER]


def fit_prompt(context: TaskContext, job: Job, render: Any, budget: int | None) -> Any:
    """The context's prompt (`render()` builds it from `context`) within the worker's prompt token budget.

    Without a budget this is `render()` and nothing else: the prompt and the context are exactly what the builder
    made. With one, the least relevant items are dropped in the task's trim order until the prompt fits; the context
    is changed in place, so everything computed from it afterwards (the citable ids a validator accepts, ranking
    hashes) matches the prompt that is sent. What was dropped is recorded on context.stats."""
    from app.services import prompt_budget

    if not budget:
        return render()
    rendered = bool(context.relevant_experience) and context.resume_text == _render(context)
    prompt, trim = prompt_budget.fit(render, trim_steps(context, job), budget)
    if trim.trimmed:
        if rendered:
            context.resume_text = _render(context)
            prompt = render()
        sent_job = 0 if (context.job_context or {}).get("structured") else estimate_tokens(job.description or "")
        selected = estimate_tokens(json.dumps(context.selected_payload(), default=str)) + sent_job
        context.stats = ContextStats.measure(max(context.stats.full_available_context_tokens, selected), selected)
        if context.job_context:
            job_selected = estimate_tokens(json.dumps(context.job_context, default=str))
            context.job_stats = jc.JobContextStats.measure(max(context.job_stats.raw_job_tokens, job_selected),
                                                           job_selected)
    context.stats.prompt_token_budget = budget
    context.stats.prompt_tokens_before_trim = trim.tokens_before
    context.stats.prompt_tokens = prompt_budget.prompt_tokens(prompt)
    context.stats.trimmed = dict(trim.removed)
    if not trim.fits:
        log.info("%s prompt is %s tokens after trimming, over its %s-token budget", context.task,
                 context.stats.prompt_tokens, budget)
    return prompt


# ───────────── builders ─────────────

def build_ranking_context(db: Session, profile: CandidateProfile, job: Job, evidence_limit: int = 40) -> TaskContext:
    """Candidate summary, target roles, relevant experience and skills, relevant verified evidence, the job.
    Unverified parsed facts are included, marked lower-confidence, when the policy allows it."""
    profile = rv.for_job(db, profile, job)  # the job's resume (else the active one), in the policy's form
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["ranking_uses_unverified"] else VERIFIED_ONLY
    context, _data, every, _resume = _base(db, profile, job, "ranking", allowed, evidence_limit)
    context = _with_job(context, jc.build_ranking_job_context(db, job, candidate=context))
    raw = rv.raw_text(profile)
    if policy["resume_source"] == "master_text":
        # The candidate chose the resume text as written: ranking reads that text, nothing from the parsed data
        # (no structured summary or background, education or certifications that could contradict it).
        context.relevant_experience, context.relevant_projects = [], []
        context.relevant_skills, context.unverified_facts = [], []
        context.education, context.certifications = [], []
        context.candidate = {k: v for k, v in context.candidate.items() if k not in ("summary", "verified_background")}
        context.resume_text = raw
    elif not context.relevant_experience:
        context.resume_text = raw  # nothing structured to select from: the resume as written
    else:
        context.resume_text = _render(context)
    _measure(context, raw, every, job)
    return context


REQUIRED_FIT_FIELDS = ("candidate_fit_score", "resume_fit_score")


def stored_fit_analysis(job: Job) -> dict[str, Any] | None:
    """The job's latest fit analysis as the Ranker stored it (app.services.ranker.legacy_fit_analysis)."""
    return job.fit_analysis or None


def is_ranked(job: Job) -> bool:
    """Whether the Ranker has completed a fit analysis for this job: the precondition for tailoring it."""
    analysis = stored_fit_analysis(job)
    return bool(analysis) and all(analysis.get(key) is not None for key in REQUIRED_FIT_FIELDS)


RANKING_REQUIRED_REASON = "not ranked yet: rank it before tailoring"


def completed_fit_analysis(db: Session, job: Job) -> dict[str, Any]:
    """The Ranker's completed FitAnalysis for this job, or ContextError. Checked, never padded with defaults."""
    analysis = stored_fit_analysis(job)
    if not analysis or any(analysis.get(key) is None for key in REQUIRED_FIT_FIELDS):
        raise ContextError("RANKING_REQUIRED", "rank this job before tailoring it: the Tailor works from the Ranker's "
                                               "completed fit analysis, and this job has none")
    ranking_id = analysis.get("ranking_id")
    if ranking_id is not None:
        ranking = db.get(JobRanking, ranking_id)
        if ranking is None or ranking.job_id != job.id or ranking.user_id != job.user_id:
            raise ContextError("FIT_ANALYSIS_MISMATCH", f"the stored fit analysis names ranking {ranking_id}, which is "
                                                        "not a ranking of this job; rank the job again")
    return analysis


def _item_evidence(db: Session, user_id: int, exclude: set[int]) -> dict[str, list[int]]:
    """Verified evidence created from resume items, by the structured item id it came from."""
    rows = db.execute(select(Evidence.id, Evidence.structured_item_id).where(
        Evidence.user_id == user_id, Evidence.verified.is_(True), Evidence.structured_item_id.is_not(None))).all()
    out: dict[str, list[int]] = {}
    for evidence_id, item_id in rows:
        if evidence_id not in exclude:
            out.setdefault(item_id, []).append(evidence_id)
    return out


def _plain_lines(profile: CandidateProfile) -> list[dict[str, Any]]:
    """The lines of a resume that has no parsed roles, as source highlights. Only used when the candidate's policy
    allows unverified resume facts: nothing in them was verified."""
    from app.services.resume_format import to_plain

    lines = [line.strip(" -*\t") for line in to_plain(profile.master_resume or "").splitlines()]
    return [{"text": line} for line in dict.fromkeys(lines) if len(line) > 20][:15]


def source_resume(context: TaskContext, item_evidence: dict[str, list[int]],
                  highlights: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The Tailor's single view of the resume: every fact once, each bullet with the one id to cite for it (the
    evidence row created from it). Projects are read-only context the summary may draw on."""
    from app.prompts.common import bullet_facts, project_facts

    roles = []
    for role in context.roles:
        ids = role.get("bullet_ids") or [None] * len(role["bullets"])
        cited = role.get("bullet_evidence_ids") or [None] * len(role["bullets"])
        bullets = [{"text": text, "evidence_ids": evidence or item_evidence.get(item_id or "", [])}
                   for text, item_id, evidence in zip(role["bullets"], ids, cited, strict=False)]
        entry = {"role_index": role["role_index"], "title": role["title"], "company": role["company"],
                 "dates": role["dates"], "bullets": bullet_facts(bullets)}
        if context.length_plan:
            entry["max_bullets"] = context.length_plan["role_caps"].get(str(role["role_index"]), 0)
        roles.append(entry)
    out: dict[str, Any] = {"summary": context.candidate.get("summary") or "", "skills": context.relevant_skills,
                           "roles": roles, "projects": project_facts(context),
                           "highlights": [] if roles else (highlights or [])}
    return {k: v for k, v in out.items() if v}


def requirement_map(analysis: dict[str, Any] | None, citable: set[int], limit: int = REQUIREMENT_MAP_LIMIT
                    ) -> list[dict[str, Any]]:
    """The Ranker's MATCH and PARTIAL_MATCH assessments as the writers read them: the requirement, its level, what the
    facts support and the citable ids behind it. REQUIRED first. MISSING ones stay out: they are gaps, sent to the
    Tailor only as gaps_to_preserve. Evidence ids the context cannot cite are dropped, never passed on."""
    rows = []
    for item in (analysis or {}).get("requirement_assessments") or []:
        if not isinstance(item, dict) or item.get("status") not in ("MATCH", "PARTIAL_MATCH"):
            continue
        ids = [i for i in item.get("evidence_ids") or [] if isinstance(i, int) and i in citable]
        rows.append({k: v for k, v in {"id": item.get("requirement_id"), "requirement": item.get("requirement"),
                                       "level": item.get("level"), "status": item["status"],
                                       "supported": item.get("supported"), "evidence_ids": ids}.items()
                     if v not in (None, "", [])})
    order = {"REQUIRED": 0, "PREFERRED": 1}
    rows.sort(key=lambda row: (order.get(row.get("level"), 2), row["status"] != "MATCH"))
    return rows[:limit]


def keyword_targets(job: Job, shown: list[str], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """The posting's ATS keywords split by what this candidate's facts support (app.services.ats_keywords.targets).

    A keyword is supported only by `shown`: the texts the prompt itself carries (its resume facts and selected
    evidence), never the whole Evidence Bank and never evidence held back for a provenance conflict, so "use" always
    means a fact in front of the model proves it. Those texts are part of what validation accepts, so a "use" term
    never fails it. A structured description parsed from an older version of the posting is not read: the keywords
    then come from the job's skills, as everywhere else a parse is stale."""
    from types import SimpleNamespace

    from app.services import ats_keywords
    from app.services.job_description import is_current
    from app.services.tailoring_results import NAME_PARTS_RE, _words

    if job.structured_description and not is_current(job):
        job = SimpleNamespace(structured_description=None, skills=job.skills, fit_analysis=job.fit_analysis)
    analysis = job.fit_analysis or {}
    names = {a.get("requirement_id"): a.get("requirement") for a in analysis.get("requirement_assessments") or []
             if isinstance(a, dict) and a.get("requirement_id")}
    words = _words(" ".join(str(text) for text in shown if text))
    vocabulary = words | {part for word in words for part in NAME_PARTS_RE.split(word) if part}
    targets = ats_keywords.targets(job, vocabulary, sources, analysis, names)
    return {k: v for k, v in targets.items() if v}


def _shown_texts(context: TaskContext) -> list[str]:
    """Every fact text a tailoring or letter prompt carries: the source resume (or the selected resume facts) and
    the selected evidence."""
    from app.prompts.common import resume_facts

    resume = context.source_resume or resume_facts(context)
    return [json.dumps(resume, ensure_ascii=False, default=str)] + [
        " ".join(str(e.get(k) or "") for k in ("company", "role", "claim")) for e in context.relevant_evidence]


def _tailoring_sources(context: TaskContext) -> list[dict[str, Any]]:
    """Every citable text of a tailoring context, {id, text}: source bullets, project bullets and evidence claims."""
    bullets = [b for part in ("roles", "projects") for unit in context.source_resume.get(part) or []
               for b in unit.get("bullets") or [] if "id" in b]
    return bullets + [{"id": e["id"], "text": e.get("claim") or ""} for e in context.relevant_evidence]


def _attach_targets(db: Session, profile: CandidateProfile, job: Job, context: TaskContext) -> None:
    context.requirement_map = requirement_map(context.fit_analysis, context.citable_ids)
    context.keyword_targets = keyword_targets(job, _shown_texts(context), _tailoring_sources(context))


def validate_tailoring_context(context: TaskContext, master_role_count: int) -> None:
    """Deterministic consistency checks before the Tailor's prompt exists. Raises ContextError."""
    if not context.resume:
        raise ContextError("TAILOR_CONTEXT_INCOMPLETE", "there is no active resume version to tailor")
    indexes = [role["role_index"] for role in context.roles]
    if len(indexes) != len(set(indexes)):
        raise ContextError("TAILOR_CONTEXT_INVALID", f"two selected roles map to the same resume role: {indexes}")
    if any(not 0 <= index < master_role_count for index in indexes):
        raise ContextError("TAILOR_CONTEXT_INVALID", "a selected role is not a role of the master resume")
    evidence_ids = [item["id"] for item in context.relevant_evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ContextError("TAILOR_CONTEXT_INVALID", "the same evidence was selected twice")
    if not (context.source_resume or context.relevant_evidence):
        raise ContextError("TAILOR_CONTEXT_INCOMPLETE", "the resume has no verified content to tailor")


def _caps_by_id(plan: Any, data: dict[str, Any]) -> dict[str, int]:
    """Each structured role's bullet budget by id, ranked by recency (app.services.resume_length)."""
    from app.services import resume_length

    roles = [r for r in data.get("experience") or [] if r.get("id") and _status(r) != "REJECTED"]
    order = sorted(range(len(roles)), key=lambda i: resume_length.recency_key(_dates(roles[i]), i))
    return {roles[index]["id"]: plan.cap(rank) for rank, index in enumerate(order)}


def _apply_plan(context: TaskContext, profile: Any, plan: Any) -> None:
    """The page target and each rendered role's budget (by role_index) on the context, and a text-source role's
    candidates cut to its budget plus CANDIDATE_SLACK."""
    from app.services import resume_length

    caps = resume_length.role_caps(plan, parse_resume(profile.master_resume or "").roles)
    context.length_plan = {**plan.as_dict(), "target": resume_length.PAGE_LABELS[plan.pages],
                           "summary_sentences": resume_length.SUMMARY_SENTENCES[plan.pages],
                           "role_caps": {str(index): cap for index, cap in caps.items()}}
    for role in context.roles:
        cap = caps.get(role["role_index"], 0)
        limit = cap + CANDIDATE_SLACK if cap else 0
        for key in ("bullets", "bullet_ids", "bullet_evidence_ids"):
            if role.get(key):
                role[key] = role[key][:limit]


def build_tailoring_context(db: Session, profile: CandidateProfile, job: Job, plan: Any = None) -> TaskContext:
    """The Tailor's canonical context: the source resume (verified facts the policy allows, each bullet with its
    evidence ids), the relevant verified evidence, the job's requirements and the Ranker's completed FitAnalysis.

    `plan` (app.services.resume_length.LengthPlan; the account's default when omitted) sets each role's bullet
    budget, newest role first: the source resume is what the Tailor chooses from, not what it must reproduce.

    Raises ContextError(RANKING_REQUIRED) without a completed FitAnalysis, UnverifiedResumeError without verified
    facts, and ContextError for inconsistent input — all before a prompt exists."""
    profile = rv.for_job(db, profile, job)  # the job's resume (else the active one), in the policy's form
    policy = candidate_policy(db, profile.user_id)
    from_text = policy["resume_source"] == "master_text"
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    analysis = completed_fit_analysis(db, job)
    from app.services import resume_length

    plan = plan or resume_length.plan_for(db, profile.user_id)
    resume = rv.resume_for(db, profile)
    caps = _caps_by_id(plan, (resume.structured_data if resume is not None else None) or {})
    context, data, every, _resume = _base(db, profile, job, "tailoring", allowed, TAILOR_EVIDENCE, caps)
    if from_text:
        # The candidate chose the master resume text as the source: its roles and bullets, used as written.
        context.fit_analysis = analysis
        context = _with_job(context, jc.build_tailoring_job_context(db, job))
        context.roles = master_text_roles(profile)
        _apply_plan(context, profile, plan)
        context.resume_text = _render(context)
        context.source_resume = source_resume(context, {}, None if context.roles else _plain_lines(profile))
        _attach_targets(db, profile, job, context)
        validate_tailoring_context(context, len(parse_resume(profile.master_resume or "").roles))
        _measure(context, rv.raw_text(profile), every, job)
        return context
    if not policy["allow_unverified_resume_facts"] and not has_verified_facts(data, every):
        raise UnverifiedResumeError(
            "Tailoring uses verified facts only. Verify your resume in Settings → Resumes (or allow unverified "
            "resume facts in your candidate policy) and try again.")
    context.fit_analysis = analysis
    if policy["allow_unverified_resume_facts"]:
        # Allowed by the candidate policy: unverified bullets are used, and stay marked as such.
        for fact in context.unverified_facts:
            role = next((r for r in context.relevant_experience
                         if r["job_title"] == fact["role"] and r["company"] == fact["company"]), None)
            if role is not None:
                role["bullets"].append({"id": fact["id"], "text": fact["text"], "type": "unverified"})
    # The FitAnalysis already says where the candidate matches; the Tailor gets requirements, not a second analysis.
    context = _with_job(context, jc.build_tailoring_job_context(db, job))
    context.roles = master_roles(profile, data, context.relevant_experience)
    _apply_plan(context, profile, plan)
    context.resume_text = _render(context)
    unstructured = not context.roles and policy["allow_unverified_resume_facts"]
    context.source_resume = source_resume(context, _item_evidence(db, profile.user_id,
                                                                  {e["id"] for e in context.excluded_evidence}),
                                          _plain_lines(profile) if unstructured else None)
    _attach_targets(db, profile, job, context)
    validate_tailoring_context(context, len(parse_resume(profile.master_resume or "").roles))
    _measure(context, rv.raw_text(profile), every, job)
    return context


def build_review_context(db: Session, profile: CandidateProfile, job: Job) -> TaskContext:
    """Structured verified truth (roles, dates, bullets, education, certifications), rejected facts, relevant verified
    evidence and CandidatePolicy, so the reviewer can check generated text against them."""
    profile = rv.for_job(db, profile, job)  # the job's resume (else the active one), in the policy's form
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, data, every, _resume = _base(db, profile, job, "review", allowed, REVIEW_EVIDENCE)
    for role in data.get("experience") or []:  # the reviewer checks every verified role's dates, not only relevant ones
        if _status(role) == "VERIFIED" and role["id"] not in {r["id"] for r in context.relevant_experience}:
            context.relevant_experience.append({"id": role["id"], "job_title": role.get("job_title") or "",
                                                "company": role.get("company") or "", "location": role.get("location") or "",
                                                "dates": _dates(role), "technologies": [], "verified": True, "bullets": []})
    context = _with_job(context, jc.build_review_job_context(db, job))
    context.rejected_facts = [{"id": u.item_id, "section": u.section, "text": u.node.get("text") or rv._label(u)}
                              for u in sr.iter_units(data) if u.status == "REJECTED"]
    context.resume_text = _render(context)
    _measure(context, rv.raw_text(profile), every, job)
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


def build_cover_letter_context(db: Session, profile: CandidateProfile, job: Job,
                               tailored: dict[str, Any] | None = None) -> TaskContext:
    """What a letter needs: the posting and its company facts, the candidate's verified proof points relevant to
    it, and the verified answers the posting raises. Verified facts only, like every other final application text."""
    profile = rv.for_job(db, profile, job)  # the job's resume (else the active one), in the policy's form
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, data, every, _resume = _base(db, profile, job, "cover_letter", allowed, LETTER_EVIDENCE)
    if not policy["allow_unverified_resume_facts"] and not has_verified_facts(data, every):
        raise UnverifiedResumeError(
            "A cover letter uses verified facts only. Verify your resume in Settings → Resumes (or allow "
            "unverified resume facts in your candidate policy) and try again.")
    personal = (data or {}).get("personal_information") or {}
    context.candidate = {**context.candidate,
                         # The letter signs off with a name: the profile's, or the one on the resume itself.
                         "name": context.candidate.get("name") or personal.get("full_name") or "",
                         "location": profile.location or personal.get("location") or "",
                         "work_authorization": profile.work_authorization or ""}
    context.job = {**context.job, "description": jc.compact_posting(normalize_description(job.description or "")),
                   **{field: getattr(job, field) for field in ("remote_type", "job_type", "seniority", "experience")
                      if getattr(job, field, None)}}
    context.knowledge = select_knowledge(db, profile.user_id, job, job_terms(job))
    context.fit_analysis = job.fit_analysis or {}
    # The posting as parsed requirements (the raw text still travels, for what it says about the company), the
    # strongest matched requirements with their proof, and the keywords the facts support.
    context.job_context = jc.build_tailoring_job_context(db, job).to_dict()
    citable = {e["id"] for e in context.relevant_evidence} | context.evidence_ids
    context.requirement_map = requirement_map(context.fit_analysis, citable, LETTER_REQUIREMENTS)
    sources = [{"id": b["evidence_ids"][0], "text": b.get("text")} for unit in
               [*context.relevant_experience, *context.relevant_projects] for b in unit.get("bullets") or []
               if b.get("evidence_ids")] + [{"id": e["id"], "text": e.get("claim")} for e in context.relevant_evidence]
    context.keyword_targets = keyword_targets(job, _shown_texts(context), sources)
    context.tailored = tailored or {}  # before measuring: the prompt carries it
    context.resume_text = _render(context)
    _measure(context, rv.raw_text(profile), every, job)
    return context


def build_followup_context(db: Session, profile: CandidateProfile, job: Job) -> TaskContext:
    """A follow-up only needs a few verified highlights relevant to the role."""
    profile = rv.for_job(db, profile, job)  # the job's resume (else the active one), in the policy's form
    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    context, _data, every, _resume = _base(db, profile, job, "followup", allowed, 5)
    context.relevant_experience = context.relevant_experience[:2]
    context.unverified_facts = context.unverified_facts if policy["allow_unverified_resume_facts"] else []
    context.resume_text = _render(context)
    _measure(context, rv.raw_text(profile), every, job)
    return context


def build_application_answer_context(db: Session, profile: CandidateProfile, question: str) -> TaskContext:
    """What answering one form question needs: the candidate's facts and policy, their verified non-sensitive
    knowledge answers, and the verified resume facts and evidence that share vocabulary with the question."""
    profile = rv.selected(db, profile)  # the active resume, in the policy's form
    from types import SimpleNamespace

    policy = candidate_policy(db, profile.user_id)
    allowed = WITH_UNVERIFIED if policy["allow_unverified_resume_facts"] else VERIFIED_ONLY
    subject = SimpleNamespace(id=None, title="", company="", location="", description=question or "", skills=[])
    context, _data, every, _resume = _base(db, profile, subject, "application_answer", allowed, 8)
    context.candidate = {k: v for k, v in context.candidate.items() if k not in ("target_roles", "preferred_locations")}
    context.relevant_experience = context.relevant_experience[:3]
    context.unverified_facts = []
    context.knowledge = select_knowledge(db, profile.user_id, subject, job_terms(subject))
    context.job = {}
    context.resume_text = _render(context)
    _measure(context, rv.raw_text(profile), every, subject)
    return context


FORM_EVIDENCE = 12
FORM_QUESTION_EVIDENCE = 4
FORM_QUESTION_ANSWERS = 3
# Profile fields a form draft never sees: only the person answers work authorization, and search settings are not
# facts about them.
FORM_PRIVATE_FIELDS = ("work_authorization", "search_preferences", "preferred_locations", "target_roles")


def _question_terms(text: str) -> JobTerms:
    from types import SimpleNamespace

    return job_terms(SimpleNamespace(title="", description=text or "", skills=[]))


@dataclass
class FormContext:
    """build_application_form_context: the candidate context; what was retrieved for each pasted question
    ({ref: {evidence_ids, knowledge_ids}}, best first); a short label for every evidence id the model may cite; the
    resume as the prompt reads it, where each verified non-bullet fact (a role, a degree, a certification, a project,
    the skills, the summary) carries a ref a draft may cite, with `resume_facts` labelling each ref; and the evidence
    ids the job's resume may draw on (None only when that scope could not be read)."""

    context: TaskContext
    questions: dict[str, dict[str, list[int]]]
    evidence_labels: dict[int, str]
    resume: dict[str, Any] = field(default_factory=dict)
    resume_facts: dict[str, str] = field(default_factory=dict)
    scope: set[int] | None = None

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return self.context.relevant_evidence

    @property
    def knowledge(self) -> list[dict[str, Any]]:
        return self.context.knowledge


def _form_resume(context: TaskContext) -> tuple[dict[str, Any], dict[str, str]]:
    """(the resume for the form prompt, {ref: label}). Bullets cite their evidence ids; every other verified fact gets
    a ref (ROLE1, PROJ1, EDU1, CERT1, SKILLS, SUMMARY) so a draft resting on it can say so."""
    facts: dict[str, str] = {}
    roles = []
    for n, r in enumerate(context.relevant_experience, start=1):
        ref = f"ROLE{n}"
        facts[ref] = ", ".join(filter(None, [r.get("job_title"), r.get("company"), r.get("dates")]))
        roles.append({"ref": ref, "job_title": r.get("job_title"), "company": r.get("company"),
                      "location": r.get("location"), "dates": r.get("dates"), "technologies": r.get("technologies") or [],
                      "bullets": [{"text": b["text"], "evidence_ids": b.get("evidence_ids") or []}
                                  for b in r.get("bullets") or []]})
    projects = []
    for n, p in enumerate(context.relevant_projects, start=1):
        ref = f"PROJ{n}"
        facts[ref] = f"Project: {p.get('name') or ''}".strip()
        projects.append({"ref": ref, "name": p.get("name"), "technologies": p.get("technologies") or [],
                         "bullets": [{"text": b["text"], "evidence_ids": b.get("evidence_ids") or []}
                                     for b in p.get("bullets") or []]})
    education = []
    for n, e in enumerate(context.education, start=1):
        facts[f"EDU{n}"] = ", ".join(str(v) for k, v in e.items() if k != "verified")
        education.append({"ref": f"EDU{n}", **e})
    certifications = []
    for n, c in enumerate(context.certifications, start=1):
        facts[f"CERT{n}"] = ", ".join(str(v) for k, v in c.items() if k != "verified")
        certifications.append({"ref": f"CERT{n}", **c})
    skills = {}
    if context.relevant_skills:
        facts["SKILLS"] = "Skills: " + ", ".join(context.relevant_skills[:12])
        skills = {"ref": "SKILLS", "names": context.relevant_skills}
    if context.candidate.get("summary"):
        facts["SUMMARY"] = "Summary: " + context.candidate["summary"][:140]
    return ({"experience": roles, "projects": projects, "skills": skills, "education": education,
             "certifications": certifications}, {k: v[:160] for k, v in facts.items()})


def build_application_form_context(db: Session, profile: CandidateProfile, job: Job,
                                   questions: list[dict[str, str]]) -> FormContext:
    """What drafting one job's application form needs, from the job's own resume (resume_verification.for_job):
    the candidate's headline, verified summary and background, the resume's roles with their verified bullets (and
    the evidence ids behind each), projects, skills, education, and verified evidence relevant to the posting.

    Each pasted question (`questions`: [{ref, question}]) also gets its own retrieval: verified evidence for that
    question (RAG_RETRIEVAL_MODE=on: hybrid retrieval with the question as the query; otherwise keyword scoring) and
    verified, non-sensitive saved answers to similar questions. Verified facts only, whatever the candidate policy.

    Scope comes before scoring in every mode: this is a new retrieval path, so evidence the job's resume may not use
    (evidence_tags.allowed_evidence_ids) is never a candidate, even with retrieval off. The other builders keep their
    old behaviour in mode off."""
    from app.services import rag_retrieval
    from app.services.evidence_provenance import excluded_ids
    from app.services.job_insights.application_form import PERSONAL_KNOWLEDGE, personal

    profile = rv.for_job(db, profile, job)
    try:
        in_scope: set[int] | None = pack_scope(db, profile, job)
    except Exception:  # noqa: BLE001  without a scope nothing outside the resume's own bullets is offered
        log.warning("resume scope for job %s could not be read", getattr(job, "id", None), exc_info=True)
        in_scope = set()
    verified = set(db.scalars(select(Evidence.id).where(Evidence.user_id == profile.user_id,
                                                        Evidence.verified.is_(True))).all())
    out_of_scope = verified - in_scope
    asked = " ".join(q.get("question") or "" for q in questions)
    terms = generation_terms(job)
    if asked:
        terms.focus |= tokens(asked) - jc.FILLER
        terms.text = f"{terms.text} {asked}"
    context, _data, every, _resume = _base(db, profile, job, "application_form", VERIFIED_ONLY, FORM_EVIDENCE,
                                           terms=terms, out_of_scope=out_of_scope)
    context.candidate = {k: v for k, v in context.candidate.items() if k not in FORM_PRIVATE_FIELDS and v}
    context.unverified_facts = []
    context.job = {}

    held_back = set(excluded_ids(db, profile.user_id))
    labels: dict[int, str] = {e["id"]: e.get("claim") or "" for e in context.relevant_evidence}
    for unit in [*context.relevant_experience, *context.relevant_projects]:
        for bullet in unit.get("bullets") or []:
            for i in bullet.get("evidence_ids") or []:
                labels.setdefault(i, bullet.get("text") or "")
    per_question: dict[str, dict[str, list[int]]] = {}
    answers: dict[int, dict[str, Any]] = {}
    for q in questions:
        text = q.get("question") or ""
        q_terms = _question_terms(text)
        deterministic, _ = select_evidence(db, profile.user_id, q_terms, FORM_QUESTION_EVIDENCE,
                                           exclude=held_back | out_of_scope)
        found = retrieved_evidence(db, profile, job, "application_form", q_terms, FORM_QUESTION_EVIDENCE,
                                   deterministic, held_back | out_of_scope, set(), query=text)
        found = [e for e in found if e["id"] in in_scope]  # a retrieval fallback never brings in another resume's
        for e in found:
            if e["id"] not in labels:
                context.relevant_evidence.append(e)
                labels[e["id"]] = e.get("claim") or ""
        suggested = [a for a in rag_retrieval.suggest_answers(db, profile.user_id, text, limit=FORM_QUESTION_ANSWERS)
                     if not a["sensitive"] and a.get("answer") and a.get("category") not in PERSONAL_KNOWLEDGE
                     and not personal(a.get("question") or "")]
        for a in suggested:
            answers.setdefault(a["id"], {"id": a["id"], "question": (a["question"] or "")[:300],
                                         "answer": a["answer"][:800]})
        per_question[q["ref"]] = {"evidence_ids": list(dict.fromkeys(e["id"] for e in found)),
                                  "knowledge_ids": [a["id"] for a in suggested]}
    context.relevant_evidence.sort(key=lambda e: e["id"])
    context.knowledge = list(answers.values())
    context.resume_text = _render(context)
    _measure(context, rv.raw_text(profile), every, job)
    resume, resume_facts = _form_resume(context)
    return FormContext(context, per_question, {i: text[:160] for i, text in labels.items()}, resume, resume_facts,
                       in_scope)


def build_inbox_context(db: Session, user_id: int, limit: int = 40) -> list[dict[str, Any]]:
    """KNOWN APPLICATIONS for inbox classification: ids, companies, roles and stages. No candidate facts."""
    rows = db.execute(select(Application.id, Job.company, Job.title, Application.status)
                      .join(Job, Job.id == Application.job_id).where(Application.user_id == user_id)
                      .order_by(desc(Application.id)).limit(limit)).all()
    return [{"application_id": r.id, "company": r.company, "role": r.title, "status": r.status} for r in rows]


def map_roles(parsed: Any, experience: list[dict[str, Any]]) -> dict[str, int]:
    """Structured role id → the index of the same role in the parsed master resume (what tailoring renders).

    Both lists are in document order, so when they hold the same number of roles they are matched by position: a
    corrected title ("SDE 3" where the text says "SDE 4") must not lose its role. Otherwise a role is matched by its
    recorded source_role_index when that still names it, then by title and employer."""
    from app.services.workflow import _best_match

    roles = [entry for entry in experience or [] if entry.get("id")]
    headers = [role.header for role in parsed.roles]
    if roles and len(roles) == len(headers):
        return {entry["id"]: index for index, entry in enumerate(roles)}
    out: dict[str, int] = {}
    used: set[int] = set()
    for entry in roles:
        label = f"{entry.get('job_title') or ''} {entry.get('company') or ''}"
        index = entry.get("source_role_index")
        if not (isinstance(index, int) and 0 <= index < len(headers) and index not in used
                and _best_match(label, [headers[index]])[1] >= 0.5):
            remaining = [h for i, h in enumerate(headers) if i not in used]
            match, score = _best_match(label, remaining) if remaining else (None, 0)
            index = headers.index(match) if match is not None and score >= 0.5 else None
        if index is not None:
            used.add(index)
            out[entry["id"]] = index
    return out


def master_text_roles(profile: CandidateProfile) -> list[dict[str, Any]]:
    """resume_source "master_text": every role of the selected resume's text with its own heading and bullets, as
    written. No bullet cites evidence; tailoring_results accepts a rewrite only when it closely restates a bullet."""
    parsed = parse_resume(profile.master_resume or "")
    return [{"role_index": index, "title": role.title, "company": role.company, "dates": role.dates,
             "bullets": [b for b in role.bullets if b.strip()], "bullet_ids": [], "bullet_evidence_ids": [],
             "verified": False}
            for index, role in enumerate(parsed.roles)]


def master_roles(profile: CandidateProfile, data: dict[str, Any], selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map selected structured roles onto the roles of the resume document tailoring renders (role_index).

    With the structured source that document is the structured data itself (structured_resume.render_document),
    so headings and dates are the person's own corrections. Rejected roles are not in the document."""
    parsed = parse_resume(profile.master_resume or "")  # the same document tailoring renders from
    by_id = {role["id"]: role for role in data.get("experience") or [] if role.get("id") and _status(role) != "REJECTED"}
    mapping = map_roles(parsed, list(by_id.values()))
    out, used = [], set()
    for role in selected:
        index = mapping.get(role["id"])
        if index is None or index in used:
            continue
        used.add(index)
        written = parsed.roles[index]
        out.append({"role_index": index, "title": written.title, "company": written.company,
                    "dates": written.dates, "bullets": [b["text"] for b in role["bullets"]],
                    "bullet_ids": [b.get("id") for b in role["bullets"]],
                    "bullet_evidence_ids": [b.get("evidence_ids") or [] for b in role["bullets"]],
                    "verified": role["verified"]})
    return sorted(out, key=lambda r: r["role_index"])


def latest_ranking_context_hash(context: TaskContext) -> dict[str, Any]:
    """The parts of a ranking context that should trigger a fresh ranking when they change."""
    return {"experience": context.relevant_experience, "projects": context.relevant_projects,
            "skills": context.relevant_skills,
            "unverified": context.unverified_facts, "candidate": context.candidate.get("summary"),
            "resume": context.resume.get("verification_status"), "evidence": context.relevant_evidence}

