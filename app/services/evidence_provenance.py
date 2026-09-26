"""Evidence provenance: does each Evidence Bank item still agree with the resume item it came from?

Evidence created from a verified resume item records where it came from (resume_id, structured_item_id) and copies
the employer and role it sat under. If those copies disagree with the source item — an achievement filed under the
wrong company or role — tailoring would attach it to the wrong job on the generated resume.

This module only diagnoses. It never edits evidence or resume data: candidate history is changed by the candidate.
Generation workers (app.services.context_builder) leave out evidence with an ERROR-level issue until it is fixed, and
the issue is listed for review (GET /api/evidence/diagnostics).

    EVIDENCE_COMPANY_MISMATCH   ERROR    the evidence's company is not the employer of its source role
    EVIDENCE_ROLE_MISMATCH      ERROR    the evidence's role is not the title of its source role
    MISSING_SOURCE_RESUME       WARNING  resume evidence whose resume no longer exists
    MISSING_SOURCE_ITEM         WARNING  the source resume no longer has the item it came from
    UNKNOWN_EMPLOYER            WARNING  manual or extracted evidence naming an employer no verified role has
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Evidence, Resume
from app.services import structured_resume as sr

ERROR, WARNING = "ERROR", "WARNING"


@dataclass(frozen=True)
class ProvenanceIssue:
    evidence_id: int
    code: str
    severity: str
    message: str
    evidence: dict[str, str]
    source: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def same_name(a: str | None, b: str | None) -> bool:
    """Loose equality for employer and title names ("Acme" and "Acme Corp." agree). Empty never disagrees."""
    a, b = _norm(a), _norm(b)
    return not a or not b or a == b or a in b or b in a


def _roles(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [r for r in (data or {}).get("experience") or [] if isinstance(r, dict)]


def diagnose(db: Session, user_id: int, *, verified_only: bool = True) -> list[ProvenanceIssue]:
    """Every provenance issue in this account's Evidence Bank. Read-only."""
    query = select(Evidence).where(Evidence.user_id == user_id)
    if verified_only:
        query = query.where(Evidence.verified.is_(True))
    rows = db.scalars(query.order_by(Evidence.id)).all()
    resumes: dict[int, Resume | None] = {}
    employers: set[str] = set()
    for resume in db.scalars(select(Resume).where(Resume.user_id == user_id)).all():
        employers |= {_norm(r.get("company")) for r in _roles(resume.structured_data) if r.get("company")}
    issues: list[ProvenanceIssue] = []
    for item in rows:
        said = {"company": item.company or "", "role": item.role or ""}
        if item.source == "resume" and item.structured_item_id:
            if item.resume_id is None:
                issues.append(ProvenanceIssue(item.id, "MISSING_SOURCE_RESUME", WARNING,
                                              "Resume evidence without a source resume", said))
                continue
            if item.resume_id not in resumes:
                resumes[item.resume_id] = db.get(Resume, item.resume_id)
            resume = resumes[item.resume_id]
            unit = sr.find_unit(resume.structured_data, item.structured_item_id) if resume is not None else None
            if unit is None:
                issues.append(ProvenanceIssue(item.id, "MISSING_SOURCE_ITEM", WARNING,
                                              "The source resume no longer has the item this evidence came from", said))
                continue
            role = unit.parent if unit.parent is not None else unit.node
            source = {"company": role.get("company") or "", "role": role.get("job_title") or ""}
            if not same_name(item.company, source["company"]):
                issues.append(ProvenanceIssue(item.id, "EVIDENCE_COMPANY_MISMATCH", ERROR,
                                              f"Filed under {item.company!r}, but its source role is at "
                                              f"{source['company']!r}", said, source))
            elif not same_name(item.role, source["role"]):
                issues.append(ProvenanceIssue(item.id, "EVIDENCE_ROLE_MISMATCH", ERROR,
                                              f"Filed under the role {item.role!r}, but its source role is "
                                              f"{source['role']!r}", said, source))
        elif item.company and employers and _norm(item.company) not in employers \
                and not any(same_name(item.company, e) for e in employers):
            issues.append(ProvenanceIssue(item.id, "UNKNOWN_EMPLOYER", WARNING,
                                          f"No resume role is at {item.company!r}", said))
    return issues


def excluded_ids(db: Session, user_id: int) -> dict[int, str]:
    """Evidence ids generation must not use yet, with the reason: ERROR-level provenance issues only."""
    return {issue.evidence_id: issue.code for issue in diagnose(db, user_id) if issue.severity == ERROR}
