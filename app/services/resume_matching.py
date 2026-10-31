"""Which of the candidate's resumes fits a job, from the resume tags. Deterministic, no model.

A resume version is tagged with one or more domains ("Backend", "Product"), each with the roles it targets ("Lead
Engineer", "Engineering Manager"; "Associate Product Manager", "Product Manager"). A new job is matched to the
best-tagged resume, a resume scoring as its best-fitting domain (several domains never add up):

    a tagged role named in the job title          → strong match (a longer role name beats a shorter one, so
                                                     "Associate Product Manager" wins over "Product Manager")
    the domain or its keywords in the title       → weaker match
    the domain's words in the description         → a tie-breaker only; never enough on its own
    no clear winner (nothing matches, or a tie)   → no choice: the active resume is used, as before

A choice pinned by the saved search or by the person always wins; re-matching only changes jobs that were matched
automatically (or never chosen).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Resume

AUTO, SETUP, USER, ACTIVE = "AUTO", "SETUP", "USER", "ACTIVE"
REMATCHABLE = (None, AUTO)

# Suggestions for the tag editor. Any domain or role can be typed; these only make tagging quick.
DOMAIN_CATALOG: dict[str, dict[str, list[str]]] = {
    "Backend": {"roles": ["Backend Engineer", "Senior Backend Engineer", "Lead Engineer", "Staff Engineer",
                          "Principal Engineer", "Engineering Manager", "Head of Engineering", "Platform Engineer"],
                "keywords": ["backend", "back-end", "platform", "api", "server", "distributed systems", "microservices"]},
    "Product": {"roles": ["Associate Product Manager", "Product Manager", "Senior Product Manager",
                          "Group Product Manager", "Director of Product", "Product Owner"],
                "keywords": ["product", "product management", "roadmap"]},
    "AI / ML": {"roles": ["Machine Learning Engineer", "AI Engineer", "Applied Scientist", "Data Scientist",
                          "ML Engineering Manager", "Research Engineer"],
                "keywords": ["machine learning", "ml", "ai", "llm", "deep learning", "nlp", "genai"]},
    "Data": {"roles": ["Data Engineer", "Senior Data Engineer", "Analytics Engineer", "Data Architect",
                       "Data Engineering Manager"],
             "keywords": ["data", "etl", "analytics", "data platform", "warehouse"]},
    "Frontend": {"roles": ["Frontend Engineer", "Senior Frontend Engineer", "UI Engineer", "Frontend Lead"],
                 "keywords": ["frontend", "front-end", "react", "ui"]},
    "Full Stack": {"roles": ["Full Stack Engineer", "Senior Full Stack Engineer"],
                   "keywords": ["full stack", "full-stack", "fullstack"]},
    "DevOps / SRE": {"roles": ["DevOps Engineer", "Site Reliability Engineer", "Cloud Engineer",
                               "Infrastructure Engineer"],
                     "keywords": ["devops", "sre", "reliability", "infrastructure", "cloud", "kubernetes"]},
    "Engineering Leadership": {"roles": ["Engineering Manager", "Senior Engineering Manager", "Director of Engineering",
                                         "VP Engineering", "Head of Engineering", "CTO"],
                               "keywords": ["engineering manager", "director of engineering", "head of engineering",
                                            "vp engineering", "cto"]},
}
ROLE_SCORE, ROLE_WORD_SCORE, DOMAIN_TITLE_SCORE, DOMAIN_TEXT_SCORE = 10, 3, 4, 1
MIN_SCORE, MIN_MARGIN = 4, 2
TEXT_WINDOW = 2000
MAX_DOMAINS, MAX_ROLES = 8, 30


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("&", " and ")).strip()


def _has(text: str, phrase: str) -> bool:
    phrase = _norm(phrase)
    return bool(phrase) and re.search(rf"(?<![\w+#]){re.escape(phrase)}(?![\w+#])", text) is not None


def keywords_of(domain: str | None) -> list[str]:
    if not domain:
        return []
    known = next((v for k, v in DOMAIN_CATALOG.items() if _norm(k) == _norm(domain)), None)
    words = [_norm(domain)] + ([_norm(w) for w in known["keywords"]] if known else [])
    return list(dict.fromkeys(w for w in words if w and w not in ("/",)))


def groups(resume: Resume) -> list[dict[str, Any]]:
    """The resume's domains, each with its roles. A resume tagged before groups existed has one: domain + roles."""
    if resume.domains is not None:
        return [{"domain": g.get("domain"), "roles": list(g.get("roles") or [])} for g in resume.domains]
    return [{"domain": resume.domain, "roles": list(resume.roles or [])}] if resume.domain or resume.roles else []


def clean_groups(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trimmed, de-duplicated groups: a domain named twice is merged, an empty group dropped, the limits applied."""
    out: dict[str, dict[str, Any]] = {}
    for group in raw:
        domain = (group.get("domain") or "").strip()[:80] or None
        roles = [r.strip()[:120] for r in group.get("roles") or [] if r and r.strip()]
        if not domain and not roles:
            continue
        into = out.setdefault(_norm(domain), {"domain": domain, "roles": []})
        into["roles"] = list(dict.fromkeys(into["roles"] + roles))[:MAX_ROLES]
    return list(out.values())[:MAX_DOMAINS]


def set_tags(resume: Resume, raw: list[dict[str, Any]]) -> None:
    """The one writer of a resume's tags: its groups, and the `domain` / `roles` mirrors older readers use."""
    cleaned = clean_groups(raw)
    resume.domains = cleaned or None
    resume.domain = next((g["domain"] for g in cleaned if g["domain"]), None)
    resume.roles = list(dict.fromkeys(r for g in cleaned for r in g["roles"]))[:MAX_ROLES] or None


def tagged(resume: Resume) -> bool:
    return bool(groups(resume))


@dataclass(frozen=True)
class Match:
    resume_id: int
    score: int
    reason: str


def score(resume: Resume, job: Any) -> Match:
    title = _norm(getattr(job, "title", ""))
    text = _norm((getattr(job, "description", "") or "")[:TEXT_WINDOW])
    best = max((_group_score(group, title, text) for group in groups(resume)), key=lambda m: m[0], default=(0, ""))
    return Match(resume.id, *best)


def _group_score(group: dict[str, Any], title: str, text: str) -> tuple[int, str]:
    domain = group.get("domain")
    best_role = max((role for role in group.get("roles") or [] if _has(title, role)), key=lambda r: len(r.split()),
                    default=None)
    points, reason = 0, ""
    if best_role:
        points += ROLE_SCORE + ROLE_WORD_SCORE * len(best_role.split())  # a longer, more specific role wins
        reason = f"role '{best_role}'"
    domain_hit = next((word for word in keywords_of(domain) if _has(title, word)), None)
    if domain_hit:
        points += DOMAIN_TITLE_SCORE
        reason = reason or f"domain '{domain}'"
    # At most 3, below MIN_SCORE: the description breaks ties but never picks a resume on its own.
    points += min(3, sum(DOMAIN_TEXT_SCORE for word in keywords_of(domain) if _has(text, word)))
    return points, reason


def pick(resumes: list[Resume], job: Any) -> Match | None:
    """The clearly best-tagged resume for a job, or None when nothing matches clearly (the active one is used)."""
    ranked = sorted((score(r, job) for r in resumes if tagged(r)), key=lambda m: m.score, reverse=True)
    if not ranked or ranked[0].score < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < MIN_MARGIN:
        return None  # two resumes fit about as well: leave it to the person
    return ranked[0]


def tagged_resumes(db: Session, user_id: int) -> list[Resume]:
    return list(db.scalars(select(Resume).where(Resume.user_id == user_id)).all())


def auto_assign(db: Session, job: Job, resumes: list[Resume] | None = None) -> Match | None:
    """Match a job that has no pinned resume. Sets resume_id and resume_choice (AUTO, or back to none). Returns the
    match. Never touches a choice the saved search or the person made. Does not commit."""
    if job.resume_choice not in REMATCHABLE:
        return None
    candidates = resumes if resumes is not None else tagged_resumes(db, job.user_id)
    match = pick(candidates, job)
    job.resume_id, job.resume_choice = (match.resume_id, AUTO) if match else (None, None)
    return match


def catalog() -> list[dict[str, Any]]:
    return [{"domain": name, "roles": data["roles"]} for name, data in DOMAIN_CATALOG.items()]
