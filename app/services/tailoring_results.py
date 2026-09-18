"""Tailored resumes produced outside the backend workers (Claude Desktop over MCP): schema and deterministic checks.

Claude writes the resume; this module decides whether it may be stored. Every check is deterministic:

* UNKNOWN_EVIDENCE        an evidence ID that is not a verified Evidence row of this account
* EVIDENCE_ROLE_MISMATCH  a role's bullet cites evidence recorded for a different employer
* UNCITED_CLAIM           a bullet with no evidence ID that is not a close restatement of a master resume bullet
* INVENTED_METRIC         a number that appears in neither the cited evidence nor the master resume
* UNSUPPORTED_TERM        a technology, employer or other name in the summary, cover letter or a screening answer
                          that appears nowhere in the master resume, the profile or verified evidence
* INVENTED_SKILL          a skill found nowhere in the master resume, the profile skills or verified evidence
* UNKNOWN_ROLE            a role_index the master resume does not have (titles, employers and dates are never taken
                          from the input, so they cannot be changed)
* WORK_AUTHORIZATION      visa / sponsorship / citizenship wording the candidate profile does not state
* LAYOUT / EMPTY          bullets in a shape the master resume cannot hold, or no content at all
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CandidateProfile, Evidence, Job
from app.services.resume_format import parse_resume, to_plain
from app.services.workflow import _best_match

# A bullet without evidence must restate a master resume bullet at least this closely (share of its words).
RESTATEMENT_THRESHOLD = 0.6
WORK_AUTH_RE = re.compile(
    r"\b(visas?|sponsor\w*|citizen\w*|permanent resident\w*|work permits?|work authori[sz]\w*|green card|"
    r"employment pass|right to work|h-?1b|pr status|residency)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
# Words as the corpus and the checked text are compared: "go" never matches inside "django", and "c++" survives.
WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")
# A sentence, line or clause boundary: the word right after one may be capitalized for grammar alone.
SEGMENT_RE = re.compile(r"[.!?:;\n]+")
# Capitalized words that carry no factual claim, so they are never treated as a named technology or employer.
PROSE_WORDS = frozenset(""" 
i a an the and or but as at in on for to of with from by we our my your their this that these those it its
dear hello hi sincerely regards best kind thank thanks thankyou yours respectfully hiring manager team role
position opening application applicant candidate resume cv company please if when while where what which who
january february march april may june july august september october november december monday tuesday
wednesday thursday friday saturday sunday am pm ai ml
""".split())  # noqa: SIM905 - a readable block, not a literal list


class BulletIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, values: Any) -> Any:
        """Evidence IDs arrive as 12 or "12" (or "E12"); anything else is a schema error."""
        if not isinstance(values, list):
            return values
        out = []
        for value in values:
            if isinstance(value, str) and re.fullmatch(r"[Ee]?\d+", value.strip()):
                value = int(value.strip().lstrip("Ee"))
            out.append(value)
        return out


class ExperienceSectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_index: int = Field(ge=0)
    bullets: list[BulletIn] = Field(min_length=1, max_length=8)


class TailoredResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    summary: str = Field(default="", max_length=1500)
    skills_order: list[str] = Field(default_factory=list, max_length=40)
    experience_sections: list[ExperienceSectionIn] = Field(default_factory=list, max_length=20)
    resume_bullets: list[BulletIn] = Field(default_factory=list, max_length=30)
    cover_letter: str = Field(default="", max_length=8000)
    screening_answer_drafts: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("skills_order")
    @classmethod
    def _skills(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def schema_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [{"code": "SCHEMA", "location": ".".join(str(p) for p in error["loc"]) or "result", "message": error["msg"]}
            for error in exc.errors()[:10]]


def _issue(code: str, location: str, message: str) -> dict[str, str]:
    return {"code": code, "location": location, "message": message}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER_RE.findall(text or "")}


def _words(text: str) -> set[str]:
    """Whole words, so a skill is matched as a word and never as a fragment of another one."""
    return {word.rstrip(".") for word in WORD_RE.findall((text or "").lower())}


def _skill_words(skill: str) -> list[str]:
    return [word for word in _words(skill) if word]


def _named_terms(text: str) -> set[str]:
    """Names a reader would read as facts: acronyms (GCP, ETL) and words capitalized mid-sentence (Bedrock).

    A word that only opens a sentence or a line is skipped: its capital says nothing about what it claims.
    """
    terms = set()
    for segment in SEGMENT_RE.split(text or ""):
        for index, token in enumerate(NAME_RE.findall(segment.strip())):
            word = token.strip(".-").lower()
            acronym = token.isupper() and len(token) > 1
            capitalized = token[:1].isupper() and any(c.islower() for c in token[1:])
            if word and word not in PROSE_WORDS and (acronym or (capitalized and index > 0)):
                terms.add(word)
    return terms


def verified_evidence(db: Session, job: Job) -> dict[int, Evidence]:
    """This account's verified evidence, by ID. Evidence of other accounts is never visible."""
    rows = db.scalars(select(Evidence).where(Evidence.user_id == job.user_id, Evidence.verified.is_(True))).all()
    return {e.id: e for e in rows}


def _same_employer(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return not a or not b or a in b or b in a


def validate_tailored_resume(db: Session, profile: CandidateProfile, job: Job, data: TailoredResumeIn, *,
                             legacy: bool = False) -> list[dict[str, str]]:
    """Every problem that stops this tailored resume from being stored. Empty means it may be saved.

    `legacy` re-checks content the backend worker produced before bullets carried evidence IDs: citation and
    metric checks are skipped for it because that content never recorded which evidence each bullet used.
    """
    issues: list[dict[str, str]] = []
    master = profile.master_resume or ""
    parsed = parse_resume(master)
    master_text = to_plain(master)
    evidence = verified_evidence(db, job)

    if not legacy and not data.experience_sections and not data.resume_bullets and not data.summary.strip():
        issues.append(_issue("EMPTY", "result", "Provide a summary and tailored bullets"))
    if not legacy and parsed.structured and data.resume_bullets:
        issues.append(_issue("LAYOUT", "resume_bullets",
                             "The master resume has roles: put bullets under experience_sections with a role_index"))
    if not legacy and not parsed.structured and data.experience_sections:
        issues.append(_issue("LAYOUT", "experience_sections",
                             "The master resume has no parsed roles: use resume_bullets instead"))

    def check_bullet(bullet: BulletIn, location: str, candidates: list[str], employer: str) -> None:
        unknown = [i for i in bullet.evidence_ids if i not in evidence]
        if unknown:
            issues.append(_issue("UNKNOWN_EVIDENCE", location,
                                 f"Evidence IDs {unknown} are not verified evidence of this account"))
        cited = [evidence[i] for i in bullet.evidence_ids if i in evidence]
        for item in cited:
            if not _same_employer(item.company, employer):
                issues.append(_issue("EVIDENCE_ROLE_MISMATCH", location,
                                     f"Evidence {item.id} is from {item.company}, not {employer}"))
        if legacy:
            return
        if not bullet.evidence_ids:
            _, score = _best_match(bullet.text, candidates)
            if score < RESTATEMENT_THRESHOLD:
                issues.append(_issue("UNCITED_CLAIM", location,
                                     "A rewritten factual bullet must cite evidence_ids or restate a master resume bullet"))
        sources = master_text + " " + " ".join(f"{e.claim} {e.context}" for e in cited)
        invented = sorted(_numbers(bullet.text) - _numbers(sources))
        if invented:
            issues.append(_issue("INVENTED_METRIC", location,
                                 f"Numbers {invented} are not in the cited evidence or the master resume"))

    for s_index, section in enumerate(data.experience_sections):
        if section.role_index >= len(parsed.roles):
            if legacy:
                continue
            issues.append(_issue("UNKNOWN_ROLE", f"experience_sections.{s_index}.role_index",
                                 f"The master resume has no role {section.role_index}"))
            continue
        role = parsed.roles[section.role_index]
        for b_index, bullet in enumerate(section.bullets):
            check_bullet(bullet, f"experience_sections.{s_index}.bullets.{b_index}", role.bullets, role.company)
    all_master_lines = [line.strip(" -\t") for line in master_text.splitlines() if len(line.strip()) > 20]
    for b_index, bullet in enumerate(data.resume_bullets):
        check_bullet(bullet, f"resume_bullets.{b_index}", all_master_lines, "")

    prefs = profile.search_preferences or {}
    evidence_text = " ".join(f"{e.company} {e.role} {e.claim} {e.context} {' '.join(e.skills or [])} "
                             f"{' '.join(e.tags or [])}" for e in evidence.values())
    # Everything the candidate can be said to have done: the resume, their own profile, and verified evidence.
    supported = _words(" ".join([master_text, evidence_text, profile.headline or "", profile.work_authorization or "",
                                 *(str(s) for s in prefs.get("primary_skills") or []),
                                 *(str(s) for s in prefs.get("secondary_skills") or [])]))
    for skill in data.skills_order:
        words = _skill_words(skill)
        if not words or not set(words) <= supported:
            issues.append(_issue("INVENTED_SKILL", "skills_order",
                                 f"'{skill}' is not in the master resume, profile skills or verified evidence"))

    # The summary, cover letter and screening answers are read as fact too, so their numbers and the names
    # they drop must come from the same places a bullet's do. Naming this job and yourself is always fine.
    allowed = supported | _words(f"{job.company} {job.title} {profile.name}")
    supported_numbers = _numbers(master_text + " " + evidence_text)
    prose = {"summary": data.summary, "cover_letter": data.cover_letter,
             **{f"screening_answer_drafts.{key}": value for key, value in data.screening_answer_drafts.items()}}
    for location, text in ({} if legacy else prose).items():
        invented = sorted(_numbers(text) - supported_numbers)
        if invented:
            issues.append(_issue("INVENTED_METRIC", location,
                                 f"Numbers {invented} are not in the master resume or your verified evidence"))
        unsupported = sorted(term for term in _named_terms(text) if term not in allowed)
        if unsupported:
            issues.append(_issue("UNSUPPORTED_TERM", location,
                                 f"{unsupported} appear nowhere in the master resume, your profile or verified "
                                 "evidence; cite them there first or leave them out"))

    authorization = (profile.work_authorization or "").lower()
    texts = {"summary": data.summary, "cover_letter": data.cover_letter,
             **{f"screening_answer_drafts.{k}": v for k, v in data.screening_answer_drafts.items()},
             **{f"experience_sections.{i}": " ".join(b.text for b in s.bullets)
                for i, s in enumerate(data.experience_sections)},
             "resume_bullets": " ".join(b.text for b in data.resume_bullets)}
    for location, text in texts.items():
        for match in {m.group(0).lower() for m in WORK_AUTH_RE.finditer(text or "")}:
            stem = match[:5]
            if not authorization or stem not in authorization:
                issues.append(_issue("WORK_AUTHORIZATION", location,
                                     f"'{match}' is not supported by the work authorization in your profile"))
    return issues


def to_variant_content(data: TailoredResumeIn, *, source: str) -> dict[str, Any]:
    """The existing ResumeVariant.content shape, so the Tailor screen, edits and exports work unchanged."""
    bullet_evidence = [{"role_index": s.role_index, "text": b.text, "evidence_ids": b.evidence_ids}
                       for s in data.experience_sections for b in s.bullets]
    bullet_evidence += [{"role_index": None, "text": b.text, "evidence_ids": b.evidence_ids} for b in data.resume_bullets]
    return {
        "summary": data.summary,
        "skills": data.skills_order,
        "experience": [{"role_index": s.role_index, "bullets": [b.text for b in s.bullets]}
                       for s in data.experience_sections],
        "experience_bullets": [b.text for b in data.resume_bullets],
        "selected_evidence_ids": sorted({i for item in bullet_evidence for i in item["evidence_ids"]}),
        "cover_letter": data.cover_letter,
        "screening_answer_drafts": data.screening_answer_drafts,
        "bullet_evidence": bullet_evidence,
        "source": source,
    }


def from_variant_content(job_id: int, content: dict[str, Any]) -> tuple[TailoredResumeIn, bool]:
    """Rebuild the checked shape from stored content; True when the content predates per-bullet evidence."""
    content = content or {}
    if "bullet_evidence" in content:
        sections: dict[int, list[dict]] = {}
        loose = []
        for item in content["bullet_evidence"]:
            bullet = {"text": item["text"], "evidence_ids": item.get("evidence_ids") or []}
            if item.get("role_index") is None:
                loose.append(bullet)
            else:
                sections.setdefault(item["role_index"], []).append(bullet)
        legacy = False
    else:
        sections = {}
        for item in content.get("experience") or []:
            if isinstance(item, dict) and isinstance(item.get("role_index"), int):
                sections.setdefault(item["role_index"], []).extend(
                    {"text": str(b), "evidence_ids": []} for b in item.get("bullets") or [] if str(b).strip())
        loose = [{"text": str(b), "evidence_ids": []} for b in content.get("experience_bullets") or [] if str(b).strip()]
        legacy = True
    data = TailoredResumeIn.model_construct(
        job_id=job_id, summary=str(content.get("summary") or ""),
        skills_order=[str(s) for s in content.get("skills") or []],
        experience_sections=[ExperienceSectionIn.model_construct(
            role_index=index, bullets=[BulletIn.model_construct(**b) for b in bullets])
            for index, bullets in sorted(sections.items())],
        resume_bullets=[BulletIn.model_construct(**b) for b in loose],
        cover_letter=str(content.get("cover_letter") or ""),
        screening_answer_drafts=dict(content.get("screening_answer_drafts") or {}))
    return data, legacy
