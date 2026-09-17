"""Resume versions, user verification, change history and the Evidence Bank link.

    Structured field → user verifies → VerifiedEvidence (Evidence, source="resume") created or updated

Rules this module enforces:
* Resume versions are never overwritten. An upload or new master text is a new version; re-uploading the same
  file reactivates its version.
* Only the user verifies. Every unit starts UNVERIFIED; editing a VERIFIED or REJECTED unit makes it EDITED and
  a VERIFIED resume NEEDS_REVIEW until it is verified again.
* A parser never overwrites data: re-parsing produces a comparison that the user resolves value by value.
* Evidence is created only from VERIFIED bullets under a VERIFIED role or project. Anything else that had
  evidence has it marked unverified (never deleted, so rankings that cited it stay explainable).
* Every change is written to ResumeDataChange with its source (PARSER, USER, SYSTEM).
"""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CandidateProfile, Evidence, Resume, ResumeDataChange, ResumeDocument
from app.services import structured_resume as sr
from app.services.structured_resume import (
    REVIEWED,
    StructuredResumeIn,
    Unit,
    iter_units,
    materialize,
)

RESUME_STATUSES = ("UNVERIFIED", "PARTIALLY_VERIFIED", "VERIFIED", "NEEDS_REVIEW")
CHANGE_FOR_STATUS = {"VERIFIED": "VERIFY", "REJECTED": "REJECT", "UNVERIFIED": "UNVERIFY"}


class ResumeConflict(RuntimeError):
    """The structured data changed since the editor loaded it."""


class UnknownItem(LookupError):
    pass


class VerificationBlocked(ValueError):
    def __init__(self, blocking: list[dict[str, Any]]):
        super().__init__(f"{len(blocking)} required item(s) still need review")
        self.blocking = blocking


def utcnow() -> datetime:
    return sr.utcnow()


def _iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()


def record(db: Session, resume: Resume, path: str, change_type: str, source: str, *, item_id: str | None = None,
           old: Any = None, new: Any = None) -> None:
    db.add(ResumeDataChange(user_id=resume.user_id, resume_id=resume.id, field_path=path[:300], item_id=item_id,
                            old_value=old, new_value=new, change_type=change_type, source=source,
                            changed_at=utcnow()))


def _touch(resume: Resume, data: dict[str, Any]) -> None:
    resume.structured_data = data  # a new object, so the JSON column is saved
    resume.revision = (resume.revision or 0) + 1
    resume.updated_at = utcnow()


# ───────────── versions ─────────────

def versions(db: Session, profile: CandidateProfile) -> list[Resume]:
    return list(db.scalars(select(Resume).where(Resume.candidate_profile_id == profile.id)
                           .order_by(desc(Resume.version))).all())


def ensure_structured(db: Session, resume: Resume) -> dict[str, Any]:
    """Structured data for a version, parsing it deterministically the first time (e.g. migrated versions)."""
    if resume.structured_data is None:
        resume.structured_data = materialize(sr.parse_deterministic(resume.raw_text or ""))
        resume.parser, resume.parser_version = "deterministic", sr.PARSER_VERSION
        db.flush()
        record(db, resume, "*", "PARSE", "PARSER", new={"parser": resume.parser, "version": resume.parser_version,
                                                        "units": sum(1 for _ in iter_units(resume.structured_data))})
    return resume.structured_data


def set_active(db: Session, resume: Resume) -> Resume:
    """Make this version the one AI workflows use. The profile's master text follows it."""
    for other in db.scalars(select(Resume).where(Resume.candidate_profile_id == resume.candidate_profile_id,
                                                 Resume.is_active.is_(True), Resume.id != resume.id)).all():
        other.is_active = False
    db.flush()
    resume.is_active = True
    if not db.scalar(select(func.count(Resume.id)).where(Resume.candidate_profile_id == resume.candidate_profile_id,
                                                         Resume.is_default.is_(True))):
        resume.is_default = True
    profile = db.get(CandidateProfile, resume.candidate_profile_id)
    if profile is not None and (profile.master_resume or "") != (resume.raw_text or ""):
        profile.master_resume = resume.raw_text or ""
    db.flush()
    return resume


def set_default(db: Session, resume: Resume) -> Resume:
    for other in db.scalars(select(Resume).where(Resume.candidate_profile_id == resume.candidate_profile_id,
                                                 Resume.is_default.is_(True), Resume.id != resume.id)).all():
        other.is_default = False
    db.flush()
    resume.is_default = True
    db.flush()
    return resume


def record_version(db: Session, profile: CandidateProfile, raw_text: str, *, document: ResumeDocument | None = None,
                   source: str = "upload", name: str | None = None, activate: bool = True) -> Resume:
    """The version for this text (and file). An identical file or text reuses its version; nothing is overwritten."""
    digest = sr.text_hash(raw_text)
    query = select(Resume).where(Resume.candidate_profile_id == profile.id)
    existing = None
    if document is not None:
        existing = db.scalar(query.where(Resume.source_file_hash == document.checksum, Resume.raw_text_hash == digest))
    if existing is None:
        existing = db.scalar(query.where(Resume.raw_text_hash == digest).order_by(desc(Resume.version)))
    if existing is not None:
        if document is not None and existing.resume_document_id is None:
            existing.resume_document_id = document.id
            existing.source_storage_path = existing.source_storage_path or document.storage_path
        ensure_structured(db, existing)
        return set_active(db, existing) if activate else existing
    version = (db.scalar(select(func.max(Resume.version)).where(Resume.candidate_profile_id == profile.id)) or 0) + 1
    resume = Resume(user_id=profile.user_id, candidate_profile_id=profile.id, version=version, source=source,
                    name=(name or (document.original_filename if document else "Master resume text"))[:300],
                    resume_document_id=document.id if document else None,
                    source_filename=document.original_filename if document else None,
                    source_media_type=document.media_type if document else None,
                    source_file_hash=document.checksum if document else None,
                    source_storage_path=document.storage_path if document else None,
                    raw_text=raw_text, raw_text_hash=digest, revision=0, is_active=False, is_default=False,
                    verification_status="UNVERIFIED", created_at=utcnow(), updated_at=utcnow())
    db.add(resume)
    db.flush()
    ensure_structured(db, resume)
    return set_active(db, resume) if activate else resume


def active_resume(db: Session, profile: CandidateProfile, create: bool = True) -> Resume | None:
    """The active version, parsed. Legacy profiles with master text but no version get one on first use."""
    resume = db.scalar(select(Resume).where(Resume.candidate_profile_id == profile.id, Resume.is_active.is_(True)))
    text = profile.master_resume or ""
    # No version yet, or the master text was changed outside this module: that text is a version of its own.
    if create and text.strip() and (resume is None or (resume.raw_text or "") != text):
        resume = _record_text_version(db, profile, text)
    if resume is not None:
        ensure_structured(db, resume)
    return resume


def _record_text_version(db: Session, profile: CandidateProfile, text: str) -> Resume:
    """record_version that tolerates another worker creating the same version at the same moment."""
    try:
        with db.begin_nested():
            return record_version(db, profile, text, source="manual_text")
    except IntegrityError:
        found = db.scalar(select(Resume).where(Resume.candidate_profile_id == profile.id,
                                               Resume.raw_text_hash == sr.text_hash(text))
                          .order_by(desc(Resume.version)))
        if found is None:
            raise
        return found


def storage_in_use(db: Session, path: str) -> bool:
    """Whether a resume version still references this stored original file."""
    return bool(db.scalar(select(func.count(Resume.id)).where(Resume.source_storage_path == path)))


# ───────────── progress and status ─────────────

def _label(unit: Unit) -> str:
    node = unit.node
    if unit.kind == "bullet":
        return node.get("text", "")[:120]
    for key in ("job_title", "name", "title", "degree", "text", "full_name"):
        if node.get(key):
            extra = node.get("company") or node.get("institution") or ""
            return f"{node[key]}{' · ' + extra if extra else ''}"[:120]
    return sr.SECTION_LABELS.get(unit.section, unit.section)


def counts(units: list[Unit]) -> dict[str, int]:
    out = dict.fromkeys(("VERIFIED", "UNVERIFIED", "EDITED", "REJECTED"), 0)
    for unit in units:
        out[unit.status] = out.get(unit.status, 0) + 1
    return out


def _state(c: dict[str, int]) -> str:
    total = sum(c.values())
    if not total:
        return "EMPTY"
    if c["EDITED"]:
        return "NEEDS_REVIEW"
    if c["VERIFIED"] + c["REJECTED"] == total:
        return "VERIFIED"
    return "PARTIAL" if c["VERIFIED"] + c["REJECTED"] else "UNVERIFIED"


def blocking_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{"item_id": u.item_id, "path": u.path, "section": u.section, "status": u.status, "label": _label(u)}
            for u in iter_units(data) if u.section in sr.REQUIRED_SECTIONS and u.status not in REVIEWED]


def progress(data: dict[str, Any] | None) -> dict[str, Any]:
    units = list(iter_units(data))
    total = counts(units)
    reviewed = total["VERIFIED"] + total["REJECTED"]
    sections = []
    for section in sr.SECTIONS:
        c = counts([u for u in units if u.section == section])
        sections.append({"section": section, "label": sr.SECTION_LABELS[section], "counts": c, "state": _state(c),
                         "required": section in sr.REQUIRED_SECTIONS})
    entries = []
    for group in ("experience", "projects"):
        for item in (data or {}).get(group) or []:
            c = counts([u for u in units if u.item_id == item["id"] or (u.parent is item)])
            entries.append({"section": group, "item_id": item["id"], "label": _label(Unit(group, item["id"], "", item)),
                            "counts": c, "state": _state(c)})
    return {"total": len(units), "counts": total, "reviewed": reviewed,
            "percent": round(100 * reviewed / len(units)) if units else 0,
            "needs_review": total["UNVERIFIED"] + total["EDITED"], "sections": sections, "entries": entries,
            "blocking": blocking_items(data)}


def refresh_status(resume: Resume, invalidated: bool = False) -> None:
    """Resume-level status. VERIFIED only comes from verify_resume; a verified resume whose facts changed, or whose
    required sections are no longer fully reviewed, NEEDS_REVIEW."""
    data = resume.structured_data
    if resume.verification_status in ("VERIFIED", "NEEDS_REVIEW"):
        if invalidated or blocking_items(data):
            resume.verification_status = "NEEDS_REVIEW"
        return
    reviewed = any(u.status in REVIEWED for u in iter_units(data))
    resume.verification_status = "PARTIALLY_VERIFIED" if reviewed else "UNVERIFIED"


# ───────────── verification ─────────────

def _set_status(unit: Unit, status: str) -> None:
    unit.node["verification"] = {"status": status, "updated_at": _iso()}


def set_item_status(db: Session, resume: Resume, item_id: str, status: str, source: str = "USER") -> dict[str, Any]:
    if status not in ("VERIFIED", "REJECTED", "UNVERIFIED"):
        raise ValueError("status must be VERIFIED, REJECTED or UNVERIFIED")
    data = copy.deepcopy(ensure_structured(db, resume))
    unit = sr.find_unit(data, item_id)
    if unit is None:
        raise UnknownItem(item_id)
    old = unit.status
    if old != status:
        _set_status(unit, status)
        record(db, resume, unit.path, CHANGE_FOR_STATUS[status], source, item_id=item_id, old=old, new=status)
        _touch(resume, data)
        refresh_status(resume, invalidated=old in REVIEWED and status == "UNVERIFIED")
        sync_evidence(db, resume)
    return data


def verify_section(db: Session, resume: Resume, section: str, source: str = "USER") -> dict[str, Any]:
    if section not in sr.SECTIONS:
        raise UnknownItem(section)
    data = copy.deepcopy(ensure_structured(db, resume))
    changed = False
    for unit in iter_units(data):
        if unit.section == section and unit.status in ("UNVERIFIED", "EDITED"):
            record(db, resume, unit.path, "VERIFY", source, item_id=unit.item_id, old=unit.status, new="VERIFIED")
            _set_status(unit, "VERIFIED")
            changed = True
    if changed:
        _touch(resume, data)
        refresh_status(resume)
        sync_evidence(db, resume)
    return data


def verify_resume(db: Session, resume: Resume, user_id: int | None) -> Resume:
    """Mark the whole resume VERIFIED. Every unit in the required sections must be VERIFIED or REJECTED."""
    data = ensure_structured(db, resume)
    blocking = blocking_items(data)
    if blocking:
        raise VerificationBlocked(blocking)
    old = resume.verification_status
    resume.verification_status, resume.verified_at, resume.verified_by = "VERIFIED", utcnow(), user_id
    record(db, resume, "*", "VERIFY_RESUME", "USER", old=old, new="VERIFIED")
    sync_evidence(db, resume)
    return resume


# ───────────── editing ─────────────

def _flat(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {"value": value}


def apply_edit(db: Session, resume: Resume, payload: StructuredResumeIn, expected_revision: int,
               source: str = "USER") -> dict[str, Any]:
    """Save edited structured data. Changed VERIFIED/REJECTED units become EDITED; new units start UNVERIFIED."""
    if expected_revision != (resume.revision or 0):
        raise ResumeConflict("This resume changed since you opened it. Reload to see the latest version.")
    before = ensure_structured(db, resume)
    after = materialize(payload, previous=before)
    old_units = {u.item_id: u for u in iter_units(before)}
    new_units = {u.item_id: u for u in iter_units(after)}
    invalidated = False
    for item_id, unit in new_units.items():
        previous = old_units.get(item_id)
        if previous is None:
            record(db, resume, unit.path, "ADD", source, item_id=item_id, new=unit.content())
            continue
        old_content, new_content = _flat(previous.content()), _flat(unit.content())
        diff = [key for key in sorted(set(old_content) | set(new_content)) if old_content.get(key) != new_content.get(key)]
        for key in diff:
            record(db, resume, f"{unit.path}.{key}", "UPDATE", source, item_id=item_id, old=old_content.get(key),
                   new=new_content.get(key))
        if diff and previous.status in ("VERIFIED", "REJECTED", "EDITED"):
            invalidated = invalidated or previous.status in REVIEWED
            _set_status(unit, "EDITED")
        if previous.path != unit.path and not diff:
            record(db, resume, unit.path, "REORDER", source, item_id=item_id, old=previous.path, new=unit.path)
    for item_id, unit in old_units.items():
        if item_id not in new_units:
            record(db, resume, unit.path, "REMOVE", source, item_id=item_id, old=unit.content())
            invalidated = invalidated or unit.status in REVIEWED
    for section in sr.LIST_SECTIONS:
        old_order = [i["id"] for i in before.get(section) or [] if i["id"] in new_units]
        new_order = [i["id"] for i in after.get(section) or [] if i["id"] in old_units]
        if old_order != new_order:
            record(db, resume, section, "REORDER", source, old=old_order, new=new_order)
    if after == before:
        return before
    if resume.pending_reparse:
        resume.pending_reparse = None  # compared against data that no longer exists
        record(db, resume, "*", "REPARSE_KEEP", "SYSTEM", old="pending comparison discarded after an edit")
    _touch(resume, after)
    refresh_status(resume, invalidated=invalidated)
    sync_evidence(db, resume)
    return after


# ───────────── Evidence Bank ─────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", sr.to_plain(text or "").lower()).strip(" .")


def fact_key(company: str, claim: str) -> str:
    return hashlib.sha256(f"{_norm(company)}|{_norm(claim)}".encode()).hexdigest()


def verified_facts(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Evidence-ready facts: VERIFIED bullets whose role or project is VERIFIED too."""
    facts = {}
    for unit in iter_units(data):
        if unit.kind != "bullet" or unit.status != "VERIFIED" or unit.parent is None:
            continue
        parent = unit.parent
        if (parent.get("verification") or {}).get("status") != "VERIFIED":
            continue
        pool = list(dict.fromkeys((parent.get("technologies") or []) + (parent.get("skills") or [])))
        if unit.section == "experience":
            company, role = parent.get("company") or "", parent.get("job_title") or ""
        else:
            company, role = "", f"Project: {parent.get('name') or ''}".strip()
        text = unit.node["text"]
        facts[unit.item_id] = {"company": company[:200], "role": role[:200], "claim": text[:2000],
                               "skills": (sr._mentions(text, pool) or pool)[:12],
                               "tags": ["achievement" if ".achievements[" in unit.path else "responsibility"]}
    return facts


def sync_evidence(db: Session, resume: Resume) -> dict[str, int]:
    """Create or update VerifiedEvidence for this version's verified facts; un-verify evidence for the rest."""
    wanted = verified_facts(resume.structured_data)
    rows = db.scalars(select(Evidence).where(Evidence.user_id == resume.user_id, Evidence.source == "resume",
                                             Evidence.resume_id == resume.id)).all()
    by_item = {row.structured_item_id: row for row in rows}
    created = updated = retired = 0
    for item_id, fact in wanted.items():
        key = fact_key(fact["company"], fact["claim"])
        row = by_item.get(item_id) or db.scalar(select(Evidence).where(
            Evidence.user_id == resume.user_id, Evidence.source == "resume", Evidence.fact_key == key)
            .order_by(Evidence.id).limit(1))
        if row is None:
            row = Evidence(user_id=resume.user_id, source="resume", verified=True, **fact)
            db.add(row)
            created += 1
        else:
            updated += 1
        for field, value in fact.items():
            setattr(row, field, value)
        row.verified, row.resume_id, row.structured_item_id = True, resume.id, item_id
        row.source_file_hash, row.fact_key = resume.source_file_hash, key
        row.context = f"Verified from resume v{resume.version}"
    for row in rows:
        if row.structured_item_id not in wanted and row.verified:
            row.verified = False
            retired += 1
    db.flush()
    return {"created": created, "updated": updated, "retired": retired}


# ───────────── re-parse: a comparison, never an overwrite ─────────────

def parse_llm(db: Session, resume: Resume) -> dict[str, Any]:
    from app.prompts.resume_parser import build_resume_parser_prompt
    from app.services.llm import LLMCall, LLMClient

    result = LLMClient.for_user(db).generate_prompt_json(build_resume_parser_prompt(resume.raw_text or ""),
                                                         LLMCall(workflow_type="profile", entity_id=resume.id))
    structured = result.get("structured_resume") if isinstance(result, dict) else None
    if not isinstance(structured, dict):
        raise ValueError("The AI parser did not return structured resume data")  # noqa: TRY004  bad model output
    return sr.from_parser_output(structured)


def _parent_path(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def reparse(db: Session, resume: Resume, use_ai: bool = False) -> dict[str, Any]:
    """Parse the raw text again and store the differences for the user to resolve. Current data is untouched."""
    current = ensure_structured(db, resume)
    source = parse_llm(db, resume) if use_ai else sr.parse_deterministic(resume.raw_text or "")
    proposed = materialize(source)
    old_units = {u.item_id: u for u in iter_units(current)}
    new_units = {u.item_id: u for u in iter_units(proposed)}
    changes: list[dict[str, Any]] = []
    for item_id, unit in new_units.items():
        previous = old_units.get(item_id)
        label = f"{sr.SECTION_LABELS[unit.section]} · {_label(unit)}"
        if previous is None:
            parent_id = unit.parent["id"] if unit.parent is not None else None
            if parent_id is not None and parent_id not in old_units:
                continue  # comes with its new parent
            changes.append({"type": "add", "path": unit.path, "item_id": item_id, "section": unit.section,
                            "parent_id": parent_id, "list": unit.path.rsplit(".", 1)[-1].split("[")[0]
                            if parent_id else unit.section, "label": label, "current": None,
                            "proposed": copy.deepcopy(unit.node), "current_status": None})
            continue
        old_content, new_content = _flat(previous.content()), _flat(unit.content())
        for key in sorted(set(old_content) | set(new_content)):
            if old_content.get(key) != new_content.get(key):
                changes.append({"type": "update", "path": f"{previous.path}.{key}", "item_id": item_id, "field": key,
                                "section": unit.section, "label": f"{label} · {key.replace('_', ' ')}",
                                "current": old_content.get(key), "proposed": new_content.get(key),
                                "current_status": previous.status})
    for item_id, unit in old_units.items():
        if item_id not in new_units and (unit.parent is None or unit.parent["id"] in new_units):
            changes.append({"type": "remove", "path": unit.path, "item_id": item_id, "section": unit.section,
                            "label": f"{sr.SECTION_LABELS[unit.section]} · {_label(unit)}",
                            "current": unit.content(), "proposed": None, "current_status": unit.status})
    for index, change in enumerate(changes):
        change["id"] = f"chg_{index + 1}"
    resume.pending_reparse = {"parser": "llm" if use_ai else "deterministic",
                              "parser_version": _parser_version(use_ai), "created_at": _iso(),
                              "base_revision": resume.revision or 0, "changes": changes}
    resume.updated_at = utcnow()
    db.flush()
    return resume.pending_reparse


def _parser_version(use_ai: bool) -> str:
    if not use_ai:
        return sr.PARSER_VERSION
    from app.prompts.resume_parser import RESUME_PARSER_PROMPT_VERSION

    return RESUME_PARSER_PROMPT_VERSION


def resolve_reparse(db: Session, resume: Resume, decisions: dict[str, str]) -> dict[str, Any]:
    """Apply the user's choice for each difference: "use_new" takes the parser value, anything else keeps the
    current one. Accepted values are UNVERIFIED: a parser result is never trusted on its own."""
    pending = resume.pending_reparse
    if not pending:
        raise UnknownItem("No re-parse comparison is waiting")
    if pending.get("base_revision") != (resume.revision or 0):
        resume.pending_reparse = None
        raise ResumeConflict("The resume changed after the re-parse. Re-parse again to compare.")
    data = copy.deepcopy(resume.structured_data)
    invalidated = accepted = 0
    for change in pending["changes"]:
        if decisions.get(change["id"]) != "use_new":
            record(db, resume, change["path"], "REPARSE_KEEP", "USER", item_id=change["item_id"],
                   old=change["current"], new=change["proposed"])
            continue
        unit = sr.find_unit(data, change["item_id"])
        if change["type"] == "update" and unit is not None:
            unit.node[change["field"]] = change["proposed"]
        elif change["type"] == "add":
            node = change["proposed"]
            parent = sr.find_unit(data, change["parent_id"]) if change["parent_id"] else None
            if change["parent_id"] and parent is None:
                continue
            target = parent.node.setdefault(change["list"], []) if parent else data.setdefault(change["section"], [])
            if not isinstance(target, list):
                continue
            target.append(node)
            unit = sr.find_unit(data, change["item_id"])
        elif change["type"] == "remove" and unit is not None:
            container = unit.parent.get(change["path"].rsplit(".", 1)[-1].split("[")[0]) if unit.parent else \
                data.get(unit.section)
            if isinstance(container, list):
                container[:] = [n for n in container if n.get("id") != change["item_id"]]
            elif unit.section == "summary":
                data["summary"]["text"] = ""
            invalidated += unit.status in REVIEWED
            unit = None
        if unit is not None:
            invalidated += unit.status in REVIEWED
            for child in [unit] + [u for u in iter_units(data) if u.parent is unit.node] if change["type"] == "add" else [unit]:
                _set_status(child, "UNVERIFIED")
        record(db, resume, change["path"], "REPARSE_ACCEPT", "PARSER", item_id=change["item_id"],
               old=change["current"], new=change["proposed"])
        accepted += 1
    resume.pending_reparse = None
    if accepted:
        normalized = materialize(StructuredResumeIn.model_validate(data), previous=data)
        resume.parser, resume.parser_version = pending.get("parser"), pending.get("parser_version")
        _touch(resume, normalized)
        refresh_status(resume, invalidated=bool(invalidated))
        sync_evidence(db, resume)
    db.flush()
    return resume.structured_data


def changes(db: Session, resume: Resume, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    query = select(ResumeDataChange).where(ResumeDataChange.resume_id == resume.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(desc(ResumeDataChange.id)).limit(limit).offset(offset)).all()
    return {"total": total, "items": [
        {"id": r.id, "field_path": r.field_path, "item_id": r.item_id, "old_value": r.old_value,
         "new_value": r.new_value, "change_type": r.change_type, "source": r.source,
         "changed_at": r.changed_at.isoformat() if r.changed_at else None} for r in rows]}
