"""Structured job descriptions: the employer's posting turned into machine-readable requirements.

    raw description (never rewritten) → normalize → parse (model, or deterministic cues) → validate schema
    → jobs.structured_description + JobRequirementEvidence rows (each with the sentence it came from)

Parsing runs as its own database-backed task (JobDescriptionParseTask), claimed by the scheduler the way ranking
tasks are. A job that cannot be parsed never blocks the pipeline: after its attempts run out the ranker falls back
to the raw description.

Rules this module enforces:
* `Job.description` is the employer's text and is only ever read here.
* Nothing is invented: every requirement carries source_text, and unknown values stay null or empty.
* REQUIRED and PREFERRED are kept apart, and so are explicit employer statements and derived signals.
* A parser never overwrites a value the user corrected: a re-parse of a corrected job becomes a candidate version
  the user resolves field by field.
"""
from __future__ import annotations

import copy
import hashlib
import html
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_JD_PARSE_STATUSES,
    REQUIREMENT_LEVELS,
    REQUIREMENT_TYPES,
    Job,
    JobDescriptionParseTask,
    JobRequirementEvidence,
    JobStructuredDataChange,
)
from app.prompts.job_description_parser import (
    JOB_DESCRIPTION_PARSER_PROMPT_VERSION,
    build_job_description_parser_prompt,
)
from app.services.lifecycle import ErrorCategory
from app.settings import get_settings

PENDING, PROCESSING, COMPLETED, FAILED = "PENDING", "PROCESSING", "COMPLETED", "FAILED"
RAW, NORMALIZED, PARSED, NEEDS_REVIEW, MANUALLY_CORRECTED = ("RAW", "NORMALIZED", "PARSED", "NEEDS_REVIEW",
                                                             "MANUALLY_CORRECTED")
PARSER_VERSION = JOB_DESCRIPTION_PARSER_PROMPT_VERSION
MAX_ERROR_LENGTH = 4000
OUTCOMES = ("parsed", "retrying", "failed", "skipped", "lost")


class ParseLost(RuntimeError):
    """The task is no longer held by this worker: its lease expired and was taken over, or it was deleted."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def description_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ───────────── normalization ─────────────

_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", re.IGNORECASE)
_ITEMS = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_BULLETS = re.compile(r"^\s*(?:[-*•●▪‣⁃◦·]|\d+[.)])\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HEADING_WORDS = ("requirement", "qualification", "responsibilit", "what you", "who you are", "about", "benefit",
                 "nice to have", "preferred", "must have", "skills", "experience", "role", "job", "we offer",
                 "perks", "compensation", "salary", "location", "how to apply", "you will", "the team")


def normalize_description(text: str) -> str:
    """The posting as clean text: HTML removed, bullets normalized, blank-line runs collapsed. Wording untouched."""
    out = _ITEMS.sub("\n- ", text or "")
    out = _BREAKS.sub("\n", out)
    out = _TAGS.sub(" ", out)
    out = html.unescape(out).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    out = _CONTROL.sub("", out)
    lines = []
    for raw in out.split("\n"):
        line = re.sub(r"[  ]{2,}", " ", raw).strip()
        lines.append(f"- {_BULLETS.sub('', line)}" if _BULLETS.match(line) else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def is_heading(line: str) -> bool:
    clean = line.strip().rstrip(":").strip()
    if not clean or len(clean) > 80 or clean.startswith("- "):
        return False
    lowered = clean.lower()
    return (line.rstrip().endswith(":") and len(clean.split()) <= 8) or (
        clean == clean.upper() and len(clean.split()) <= 8 and any(ch.isalpha() for ch in clean)) or (
        len(clean.split()) <= 6 and any(word in lowered for word in HEADING_WORDS) and not clean.endswith("."))


def sections_of(text: str) -> list[tuple[str, list[str]]]:
    """(heading, lines) for each section of the posting; lines before the first heading sit under ""."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if is_heading(line):
            sections.append((line.strip().rstrip(":").strip(), []))
        else:
            sections[-1][1].append(line.strip())
    return [(heading, lines) for heading, lines in sections if lines]


def sentences(line: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line.lstrip("- ").strip())
    return [p.strip() for p in parts if p.strip()]


# ───────────── schema ─────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class JobIdentityIn(_In):
    title: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    employment_type: str | None = Field(default=None, max_length=60)
    remote_type: str | None = Field(default=None, max_length=40)
    seniority: str | None = Field(default=None, max_length=100)


class ExperienceRequirementsIn(_In):
    minimum_years: float | None = Field(default=None, ge=0, le=60)
    maximum_years: float | None = Field(default=None, ge=0, le=60)
    leadership_required: bool | None = None
    management_required: bool | None = None


class LocationRequirementsIn(_In):
    country: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    relocation_supported: bool | None = None
    visa_sponsorship: bool | None = None
    work_authorization_required: bool | None = None


class CompensationIn(_In):
    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=10)
    period: str | None = Field(default=None, max_length=20)
    source_text: str | None = Field(default=None, max_length=1000)
    source: str | None = Field(default=None, max_length=20)  # job_description | job_board


class DerivedSignalsIn(_In):
    likely_interview_domains: list[str] = Field(default_factory=list, max_length=20)
    likely_system_design_topics: list[str] = Field(default_factory=list, max_length=20)
    likely_leadership_topics: list[str] = Field(default_factory=list, max_length=20)
    likely_business_topics: list[str] = Field(default_factory=list, max_length=20)


Phrases = Field(default_factory=list, max_length=60)


class StructuredJobDescriptionIn(_In):
    job_identity: JobIdentityIn = Field(default_factory=JobIdentityIn)
    role_summary: str | None = Field(default=None, max_length=4000)
    responsibilities: list[str] = Phrases
    required_skills: list[str] = Phrases
    preferred_skills: list[str] = Phrases
    experience_requirements: ExperienceRequirementsIn = Field(default_factory=ExperienceRequirementsIn)
    technical_domains: list[str] = Phrases
    leadership_expectations: list[str] = Phrases
    education_requirements: list[str] = Phrases
    location_requirements: LocationRequirementsIn = Field(default_factory=LocationRequirementsIn)
    compensation: CompensationIn = Field(default_factory=CompensationIn)
    hard_requirements: list[str] = Phrases
    nice_to_have: list[str] = Phrases
    keywords: list[str] = Field(default_factory=list, max_length=80)
    company_context: list[str] = Phrases
    derived_signals: DerivedSignalsIn = Field(default_factory=DerivedSignalsIn)


class RequirementIn(_In):
    id: str | None = Field(default=None, max_length=60)
    type: str = Field(default="OTHER", max_length=25)
    normalized_value: str = Field(max_length=300)
    requirement_level: str = Field(default="UNKNOWN", max_length=15)
    source_text: str = Field(default="", max_length=4000)
    source_section: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=0.5, ge=0, le=1)
    is_explicit: bool = True
    is_derived: bool = False

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> str:
        clean = str(value or "").strip().upper()
        return clean if clean in REQUIREMENT_TYPES else "OTHER"

    @field_validator("requirement_level", mode="before")
    @classmethod
    def _level(cls, value: Any) -> str:
        clean = str(value or "").strip().upper()
        return clean if clean in REQUIREMENT_LEVELS else "UNKNOWN"


class ParsedJobDescription(_In):
    structured_description: StructuredJobDescriptionIn = Field(default_factory=StructuredJobDescriptionIn)
    requirements: list[RequirementIn] = Field(default_factory=list, max_length=200)


# ───────────── deterministic extraction ─────────────

REQUIRED_CUES = ("required", "must have", "must be", "you have", "you must", "essential", "minimum of", "at least",
                 "we require", "is a requirement", "proven", "demonstrated", "strong experience")
PREFERRED_CUES = ("nice to have", "preferred", "a plus", "bonus", "ideally", "desirable", "would be great",
                  "familiarity with", "exposure to", "good to have", "advantage")
LEADERSHIP_CUES = ("lead", "leading", "mentor", "manage", "management", "hiring", "coach", "line manager",
                   "direct reports", "stakeholder")
EDUCATION_CUES = ("degree", "bachelor", "master", "phd", "b.tech", "b.e.", "m.tech", "computer science")
VISA_CUES = ("visa", "sponsorship", "sponsor", "work permit", "work authorization", "work authorisation",
             "right to work")
LOCATION_CUES = ("onsite", "on-site", "hybrid", "remote", "relocate", "relocation", "based in", "office")
RESPONSIBILITY_CUES = ("you will", "responsible for", "own", "build", "design", "deliver", "drive", "partner with",
                       "collaborate")
COMPANY_CUES = ("we are", "our mission", "our team is", "founded", "our customers", "about us", "we believe")
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*years?", re.IGNORECASE)
SALARY_RE = re.compile(
    r"(?P<currency>[$€£₹]|USD|EUR|GBP|INR|SGD)\s?(?P<min>\d[\d,]*(?:\.\d+)?)\s?(?P<min_unit>[kKmM]|lakh|lpa)?"
    r"(?:\s*(?:-|–|to)\s*(?P<currency2>[$€£₹])?\s?(?P<max>\d[\d,]*(?:\.\d+)?)\s?(?P<max_unit>[kKmM]|lakh|lpa)?)?",
    re.IGNORECASE)
PERIOD_WORDS = {"year": "year", "annum": "year", "annual": "year", "yearly": "year", "month": "month",
                "hour": "hour", "week": "week", "day": "day"}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
# Words that name work rather than a requirement of the candidate.
SECTION_LEVEL = {"requirement": "REQUIRED", "qualification": "REQUIRED", "must have": "REQUIRED",
                 "what you": "REQUIRED", "who you are": "REQUIRED", "nice to have": "PREFERRED",
                 "preferred": "PREFERRED", "bonus": "PREFERRED", "responsibilit": "INFORMATIONAL",
                 "you will": "INFORMATIONAL", "about": "INFORMATIONAL", "benefit": "INFORMATIONAL",
                 "perks": "INFORMATIONAL", "we offer": "INFORMATIONAL", "the team": "INFORMATIONAL"}


def _level_for(text: str, heading: str) -> str:
    lowered = text.lower()
    if any(cue in lowered for cue in PREFERRED_CUES):
        return "PREFERRED"
    if any(cue in lowered for cue in REQUIRED_CUES):
        return "REQUIRED"
    for word, level in SECTION_LEVEL.items():
        if word in heading.lower():
            return level
    return "UNKNOWN"


def _type_for(text: str) -> str:
    lowered = f" {text.lower()} "
    if any(cue in lowered for cue in VISA_CUES):
        return "VISA"
    if any(cue in lowered for cue in EDUCATION_CUES):
        return "EDUCATION"
    if YEARS_RE.search(text):
        return "EXPERIENCE"
    if any(cue in lowered for cue in LEADERSHIP_CUES):
        return "LEADERSHIP"
    if any(cue in lowered for cue in LOCATION_CUES):
        return "LOCATION"
    if SALARY_RE.search(text) and any(word in lowered for word in ("salary", "compensation", "package", "ctc")):
        return "COMPENSATION"
    if any(cue in lowered for cue in COMPANY_CUES):
        return "OTHER"
    if any(cue in lowered for cue in RESPONSIBILITY_CUES):
        return "RESPONSIBILITY"
    return "SKILL"


def _amount(value: str | None, unit: str | None) -> float | None:
    if not value:
        return None
    number = float(value.replace(",", ""))
    unit = (unit or "").lower()
    if unit in ("k",):
        number *= 1_000
    elif unit in ("m",):
        number *= 1_000_000
    elif unit in ("lakh", "lpa"):
        number *= 100_000
    return number


def extract_compensation(text: str) -> dict[str, Any]:
    """Salary the posting itself states. Never an estimate: no match means no compensation."""
    for line in (text or "").splitlines():
        lowered = line.lower()
        if not any(word in lowered for word in ("salary", "compensation", "pay", "package", "ctc", "base")):
            continue
        match = SALARY_RE.search(line)
        if not match:
            continue
        currency = match.group("currency") or ""
        period = next((PERIOD_WORDS[word] for word in PERIOD_WORDS if word in lowered), None)
        return {"min": _amount(match.group("min"), match.group("min_unit")),
                "max": _amount(match.group("max"), match.group("max_unit")),
                "currency": CURRENCY_SYMBOLS.get(currency, currency.upper() or None), "period": period,
                "source_text": line.strip()[:1000], "source": "job_description"}
    return {}


def deterministic_parse(job: Any, text: str) -> dict[str, Any]:
    """Parse with cue words only, no model. Used by the mock provider and as the floor for what must be extracted."""
    structured = StructuredJobDescriptionIn().model_dump()
    structured["job_identity"] = {"title": getattr(job, "title", None), "company": getattr(job, "company", None),
                                 "location": getattr(job, "location", None),
                                 "employment_type": getattr(job, "job_type", None),
                                 "remote_type": getattr(job, "remote_type", None),
                                 "seniority": getattr(job, "seniority", None)}
    requirements: list[dict[str, Any]] = []
    summary_lines: list[str] = []
    for heading, lines in sections_of(text):
        for line in lines:
            for sentence in sentences(line):
                if len(sentence) < 12:
                    continue
                level, kind = _level_for(sentence, heading), _type_for(sentence)
                if kind in ("COMPENSATION", "OTHER"):
                    level = "INFORMATIONAL"  # pay and company blurb are not asks of the candidate
                if not summary_lines and not heading:
                    summary_lines.append(sentence)
                entry = {"type": kind, "normalized_value": sentence[:300], "requirement_level": level,
                         "source_text": sentence[:4000], "source_section": heading or None,
                         "confidence": 0.9 if level in ("REQUIRED", "PREFERRED") else 0.5,
                         "is_explicit": True, "is_derived": False}
                requirements.append(entry)
                target = ("required_skills" if level == "REQUIRED" and kind == "SKILL" else
                          "preferred_skills" if level == "PREFERRED" and kind == "SKILL" else
                          "responsibilities" if kind == "RESPONSIBILITY" else
                          "leadership_expectations" if kind == "LEADERSHIP" else
                          "education_requirements" if kind == "EDUCATION" else
                          "company_context" if kind == "OTHER" else None)
                if target and sentence not in structured[target]:
                    structured[target].append(sentence[:300])
                if level == "REQUIRED" and kind in ("EXPERIENCE", "EDUCATION", "VISA", "LOCATION"):
                    structured["hard_requirements"].append(sentence[:300])
                elif level == "PREFERRED":
                    structured["nice_to_have"].append(sentence[:300])
                if kind == "EXPERIENCE" and (years := YEARS_RE.search(sentence)):
                    minimum, maximum = years.group(1), years.group(2)
                    structured["experience_requirements"]["minimum_years"] = float(minimum)
                    structured["experience_requirements"]["maximum_years"] = float(maximum) if maximum else None
                if kind == "LEADERSHIP" and level == "REQUIRED":
                    structured["experience_requirements"]["leadership_required"] = True
    for key, value in structured.items():
        if isinstance(value, list):
            structured[key] = list(dict.fromkeys(value))[:60]
    structured["role_summary"] = " ".join(summary_lines)[:4000] or None
    structured["compensation"] = {**structured["compensation"], **extract_compensation(text)}
    return {"structured_description": structured, "requirements": requirements}


# ───────────── persistence ─────────────

def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """Structured data as {field_path: value}, so changes can be recorded field by field."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flat(item, f"{prefix}.{key}" if prefix else key))
    else:
        out[prefix] = value
    return out


def record_change(db: Session, job: Job, path: str, old: Any, new: Any, source: str) -> None:
    db.add(JobStructuredDataChange(user_id=job.user_id, job_id=job.id, field_path=path[:300], old_value=old,
                                   new_value=new, change_source=source, created_at=utcnow()))


def corrected_paths(db: Session, job: Job) -> set[str]:
    """Field paths the user corrected: a parser never overwrites these."""
    rows = db.scalars(select(JobStructuredDataChange.field_path)
                      .where(JobStructuredDataChange.job_id == job.id,
                             JobStructuredDataChange.change_source == "USER")).all()
    return set(rows)


def store_requirements(db: Session, job: Job, requirements: list[RequirementIn]) -> list[JobRequirementEvidence]:
    """Replace this job's requirement evidence with the current parse."""
    for row in db.scalars(select(JobRequirementEvidence).where(JobRequirementEvidence.job_id == job.id)).all():
        db.delete(row)
    db.flush()
    stored = []
    for index, item in enumerate(requirements, start=1):
        if not item.normalized_value:
            continue
        row = JobRequirementEvidence(
            user_id=job.user_id, job_id=job.id, item_id=item.id or f"req_{index:03d}", requirement_type=item.type,
            normalized_value=item.normalized_value[:300], requirement_level=item.requirement_level,
            source_text=item.source_text or "", source_section=item.source_section,
            confidence=float(item.confidence), is_explicit=bool(item.is_explicit) and not item.is_derived,
            is_derived=bool(item.is_derived), created_at=utcnow(), updated_at=utcnow())
        db.add(row)
        stored.append(row)
    return stored


def store_parse(db: Session, job: Job, parsed: ParsedJobDescription, *, source: str = "PARSER",
                version: str = PARSER_VERSION) -> dict[str, Any]:
    """Save a parse, keeping any field the user corrected and recording every change."""
    structured = parsed.structured_description.model_dump()
    previous = job.structured_description or {}
    protected = corrected_paths(db, job) if job.structured_description_status == MANUALLY_CORRECTED else set()
    old_flat, new_flat = _flat(previous), _flat(structured)
    for path in sorted(set(old_flat) | set(new_flat)):
        if path in protected:
            # A value the user corrected: keep theirs, and say so in the history.
            keys = path.split(".")
            target = structured
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = old_flat.get(path)
            continue
        if old_flat.get(path) != new_flat.get(path):
            record_change(db, job, path, old_flat.get(path), new_flat.get(path), source)
    job.structured_description = structured
    job.structured_description_version = version
    job.structured_description_parsed_at = utcnow()
    job.structured_description_hash = description_hash(job.description or "")
    job.structured_description_status = MANUALLY_CORRECTED if protected else PARSED
    store_requirements(db, job, parsed.requirements)
    return structured


def normalize_job(db: Session, job: Job) -> str:
    """Keep the cleaned posting text beside the raw one. The raw description is never changed."""
    text = normalize_description(job.description or "")
    if (job.normalized_description or "") != text:
        job.normalized_description = text
        if job.structured_description_status == RAW:
            job.structured_description_status = NORMALIZED
    return text


def parse_job_description(db: Session, job: Job, *, user_id: int | None = None) -> ParsedJobDescription:
    """Parse one posting with the account's model. Board metadata fills what the posting does not state."""
    from app.services.llm import LLMCall, LLMClient

    text = normalize_job(db, job)
    if not text.strip():
        raise ValueError("This job has no description to parse")
    prompt = build_job_description_parser_prompt(job, text, [heading for heading, _ in sections_of(text) if heading])
    raw = LLMClient.for_user(db, user_id or job.user_id).generate_prompt_json(
        prompt, LLMCall(workflow_type="job", entity_id=job.id))
    parsed = validated(raw if isinstance(raw, dict) else {})
    return with_board_facts(job, parsed)


def validated(raw: dict[str, Any]) -> ParsedJobDescription:
    """Parser output as a ParsedJobDescription, trimmed to the schema's list limits so a long posting still parses."""
    from app.services.structured_resume import within_limits

    body = dict(raw)
    body["structured_description"] = within_limits(StructuredJobDescriptionIn, body.get("structured_description") or {})
    body["requirements"] = (body.get("requirements") or [])[:200]
    return ParsedJobDescription.model_validate(body)


def with_board_facts(job: Job, parsed: ParsedJobDescription) -> ParsedJobDescription:
    """Fill identity and salary the job board reported when the posting text does not state them. Employer salary
    already in the structured data is never replaced (external estimates never reach this field)."""
    identity = parsed.structured_description.job_identity
    for field, value in (("title", job.title), ("company", job.company), ("location", job.location),
                         ("remote_type", job.remote_type), ("employment_type", job.job_type),
                         ("seniority", job.seniority)):
        if not getattr(identity, field) and value:
            setattr(identity, field, value)
    salary = parsed.structured_description.compensation
    if salary.min is None and salary.max is None and (job.salary_min or job.salary_max):
        salary.min, salary.max = job.salary_min, job.salary_max
        salary.currency = salary.currency or job.salary_currency
        salary.period = salary.period or job.salary_interval
        salary.source = "job_board"
        salary.source_text = salary.source_text or f"Salary reported by {job.source_board or job.source or 'the board'}"
    elif salary.min is not None or salary.max is not None:
        salary.source = salary.source or "job_description"
    return parsed


# ───────────── the parse queue ─────────────

def needs_parse(job: Job) -> bool:
    """True when the job has a description that has not been parsed in its current wording."""
    if not (job.description or "").strip():
        return False
    if job.structured_description_status in (PARSED, MANUALLY_CORRECTED):
        return job.structured_description_hash != description_hash(job.description or "")
    return True


def queue_parse(db: Session, job: Job, *, reason: str = "new", now: datetime | None = None) -> JobDescriptionParseTask | None:
    """Queue one parse for this job. A job never has two active tasks, and the same text is never queued twice."""
    if not (job.description or "").strip():
        return None
    digest = description_hash(job.description or "")
    # A task this session created but has not flushed yet counts too: sessions do not autoflush, so without this
    # the same job could be queued twice in one transaction and break the caller's commit.
    queued = next((task for task in db.new
                   if isinstance(task, JobDescriptionParseTask) and task.job_id == job.id), None)
    if queued is not None:
        return queued
    active = db.scalar(select(JobDescriptionParseTask).where(
        JobDescriptionParseTask.job_id == job.id, JobDescriptionParseTask.status.in_(ACTIVE_JD_PARSE_STATUSES)))
    if active is not None:
        return active
    task = JobDescriptionParseTask(user_id=job.user_id, job_id=job.id, description_hash=digest, status=PENDING,
                                  reason=reason, attempt_count=0, created_at=now or utcnow())
    db.add(task)
    return task


def sweep(db: Session, limit: int | None = None) -> dict[str, int]:
    """Queue parses for postings that have never been parsed, oldest first. Commits.

    A posting whose text changes is queued by whoever changed it (app.services.sources.upsert_job, the re-parse
    endpoint); this sweep only picks up postings that were never parsed at all, including the existing backlog.
    """
    limit = limit or get_settings().jd_parse_enqueue_batch_size
    active = select(JobDescriptionParseTask.job_id).where(JobDescriptionParseTask.status.in_(ACTIVE_JD_PARSE_STATUSES))
    candidates = db.scalars(
        select(Job)
        .where(Job.description.is_not(None), Job.description != "",
               Job.structured_description_status.in_((RAW, NORMALIZED)), Job.id.not_in(active))
        .order_by(Job.id).limit(max(1, limit))).all()
    queued = 0
    for job in candidates:
        if not needs_parse(job):
            continue
        try:  # another worker may queue the same job at the same moment
            with db.begin_nested():
                if queue_parse(db, job) is not None:
                    queued += 1
        except IntegrityError:
            continue
    db.commit()
    return {"queued": queued, "waiting": len(candidates) - queued}


def claim(db: Session, *, worker_id: str, limit: int, now: datetime | None = None,
          lease_seconds: int | None = None) -> list[int]:
    """Lease up to `limit` due tasks with FOR UPDATE SKIP LOCKED. Returns their ids. Commits."""
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or get_settings().jd_parse_lease_seconds)
    query = (select(JobDescriptionParseTask)
             .where(JobDescriptionParseTask.status == PENDING,
                    or_(JobDescriptionParseTask.next_attempt_at.is_(None),
                        JobDescriptionParseTask.next_attempt_at <= now))
             .order_by(JobDescriptionParseTask.next_attempt_at.asc().nulls_first(), JobDescriptionParseTask.id)
             .limit(max(1, limit)))
    if db.get_bind().dialect.name == "postgresql":  # two workers never receive the same task
        query = query.with_for_update(skip_locked=True, of=JobDescriptionParseTask)
    tasks = db.scalars(query.execution_options(populate_existing=True)).all()
    claimed = []
    for task in tasks:
        task.status, task.locked_by = PROCESSING, worker_id[:200]
        task.lock_token, task.lock_expires_at = uuid.uuid4().hex, now + lease
        task.attempt_count += 1
        task.started_at, task.next_attempt_at = now, None
        claimed.append(task.id)
    db.commit()
    return claimed


def recover_expired(db: Session, now: datetime | None = None) -> int:
    """Tasks whose worker died go back in the queue."""
    now = now or utcnow()
    rows = db.scalars(select(JobDescriptionParseTask).where(
        JobDescriptionParseTask.status == PROCESSING, JobDescriptionParseTask.lock_expires_at < now)).all()
    for task in rows:
        task.status, task.locked_by, task.lock_token, task.lock_expires_at = PENDING, None, None, None
        task.last_error = "Interrupted: the worker stopped while parsing this description"
        task.error_category = ErrorCategory.LEASE_EXPIRED
    db.commit()
    return len(rows)


def _finish(task: JobDescriptionParseTask, status: str, now: datetime, error: str | None = None,
            category: ErrorCategory | None = None) -> None:
    task.status, task.completed_at = status, now
    task.locked_by = task.lock_token = task.lock_expires_at = None
    task.last_error, task.error_category = (error[:MAX_ERROR_LENGTH] if error else None), category
    task.next_attempt_at = None


def process(sessions: Any, task_id: int, worker_id: str = "inline") -> str:
    """Parse one claimed task in a session bound to its owner. Returns the outcome."""
    from app.services.llm import error_type

    with sessions() as db:
        task = db.get(JobDescriptionParseTask, task_id)
        if task is None or task.status != PROCESSING:
            return "lost"
        db.info["user_id"] = task.user_id
        job = db.get(Job, task.job_id)
        now = utcnow()
        if job is None:
            _finish(task, FAILED, now, "The job was deleted")
            db.commit()
            return "skipped"
        try:
            parsed = parse_job_description(db, job, user_id=task.user_id)
            store_parse(db, job, parsed)
            _finish(task, COMPLETED, now)
            db.commit()
            return "parsed"
        except Exception as exc:  # noqa: BLE001  one bad posting never stops the worker
            db.rollback()
            task = db.get(JobDescriptionParseTask, task_id)
            job = db.get(Job, task.job_id) if task else None
            message = f"{type(exc).__name__}: {exc}"
            category = ErrorCategory.VALIDATION if error_type(exc) == "SCHEMA_VALIDATION" else ErrorCategory.EXTERNAL_SERVICE
            settings = get_settings()
            if task.attempt_count >= settings.jd_parse_max_attempts:
                _finish(task, FAILED, now, message, category)
                if job is not None and job.structured_description_status in (RAW, NORMALIZED):
                    job.structured_description_status = NEEDS_REVIEW
                db.commit()
                return "failed"
            task.status, task.locked_by, task.lock_token, task.lock_expires_at = PENDING, None, None, None
            task.last_error, task.error_category = message[:MAX_ERROR_LENGTH], category
            task.next_attempt_at = now + timedelta(seconds=settings.jd_parse_retry_seconds * task.attempt_count)
            db.commit()
            return "retrying"


def parse_now(db: Session, job: Job) -> ParsedJobDescription:
    """Parse and store one job in the caller's session (manual re-parse, tests)."""
    parsed = parse_job_description(db, job)
    store_parse(db, job, parsed)
    return parsed


# ───────────── waiting, for the ranker ─────────────

def waiting_reason(db: Session, job: Job) -> str | None:
    """Why ranking should wait for this job's description to be parsed, or None to go ahead.

    Ranking waits only while a parse is actually queued or running and has attempts left. A failed or exhausted
    parse never holds a job back: the ranker falls back to the raw description.
    """
    if not get_settings().jd_parse_enabled or not needs_parse(job):
        return None
    task = db.scalar(select(JobDescriptionParseTask).where(JobDescriptionParseTask.job_id == job.id)
                     .order_by(desc(JobDescriptionParseTask.id)).limit(1))
    if task is None or task.status in (COMPLETED, FAILED):
        return None
    if task.attempt_count >= get_settings().jd_parse_max_attempts:
        return None
    return "The job description is still being parsed"


# ───────────── manual correction and re-parse ─────────────

def apply_correction(db: Session, job: Job, payload: StructuredJobDescriptionIn) -> dict[str, Any]:
    """Save the user's corrections to the structured description. Corrected paths are protected from parsers."""
    old = job.structured_description or StructuredJobDescriptionIn().model_dump()
    new = payload.model_dump()
    old_flat, new_flat = _flat(old), _flat(new)
    changed = [path for path in sorted(set(old_flat) | set(new_flat)) if old_flat.get(path) != new_flat.get(path)]
    for path in changed:
        record_change(db, job, path, old_flat.get(path), new_flat.get(path), "USER")
    if changed:
        job.structured_description = new
        job.structured_description_status = MANUALLY_CORRECTED
    return job.structured_description


def reparse(db: Session, job: Job) -> dict[str, Any]:
    """Parse the posting again.

    A job the user corrected keeps its values: the new parse is stored as a candidate with a field-by-field
    comparison to resolve. Anything else is simply re-parsed.
    """
    parsed = parse_job_description(db, job)
    if job.structured_description_status != MANUALLY_CORRECTED or not job.structured_description:
        store_parse(db, job, parsed)
        job.structured_description_candidate = None
        return {"applied": True, "structured_description": job.structured_description, "changes": []}
    current, proposed = _flat(job.structured_description), _flat(parsed.structured_description.model_dump())
    protected = corrected_paths(db, job)
    changes = [{"id": f"chg_{index}", "path": path, "current": current.get(path), "proposed": proposed.get(path),
                "corrected": path in protected}
               for index, path in enumerate(sorted(set(current) | set(proposed)), start=1)
               if current.get(path) != proposed.get(path)]
    job.structured_description_candidate = {
        "version": PARSER_VERSION, "created_at": utcnow().isoformat(), "changes": changes,
        "structured_description": parsed.structured_description.model_dump(),
        "requirements": [item.model_dump() for item in parsed.requirements]}
    return {"applied": False, "structured_description": job.structured_description, "changes": changes}


def resolve_reparse(db: Session, job: Job, decisions: dict[str, str]) -> dict[str, Any]:
    """Apply the user's choice for each difference a re-parse found. "use_new" takes the parser's value."""
    candidate = job.structured_description_candidate
    if not candidate:
        raise ValueError("No re-parse is waiting for this job")
    structured = copy.deepcopy(job.structured_description or {})  # a new object: JSON columns save by identity
    accepted = 0
    for change in candidate["changes"]:
        if decisions.get(change["id"]) != "use_new":
            continue
        keys = change["path"].split(".")
        target = structured
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = change["proposed"]
        record_change(db, job, change["path"], change["current"], change["proposed"], "PARSER")
        accepted += 1
    job.structured_description = structured
    job.structured_description_hash = description_hash(job.description or "")
    job.structured_description_parsed_at = utcnow()
    if accepted:
        store_requirements(db, job, [RequirementIn.model_validate(item) for item in candidate["requirements"]])
    job.structured_description_candidate = None
    return {"accepted": accepted, "structured_description": structured}
