"""Which resume a piece of evidence belongs to (evidence_resume_links), and which evidence a job may use.

    AUTO links    written when a resume's facts are verified (resume_verification.sync_evidence) or extracted from it
    USER links    a person's own tags (Evidence Bank); a person's removal of a resume fact's link is kept as `excluded`,
                  so verification never adds it back — the same rule as resume pins: a person's choice wins. Other
                  evidence is linked by automation only once (extraction), so removing its tag just deletes the link

The scope rule job-specific retrieval applies before any scoring (`allowed_evidence_ids`), for one resume R:

    verified evidence e is usable for R  ⇔
        e is excluded from R by a person                         → no
        e has a link to R (AUTO or USER)                         → yes
        e came from a resume (source = resume)                   → yes only if R itself verifies the same fact
                                                                   (fact_key), never because another resume did
        e is linked to some other resume                         → no
        otherwise (manual or extracted evidence with no tag)     → yes: untagged evidence applies to every resume

So a fact verified only on resume A can never reach a job that uses resume B, however similar it is, and a fact a
person tagged to B reaches B's jobs. This module never edits Evidence itself.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Evidence, EvidenceResumeLink, Resume, now

AUTO, USER = "AUTO", "USER"


class TagError(ValueError):
    """The tags asked for cannot be set (unknown evidence or a resume of another account)."""


def verified_fact_keys(resume: Resume | None) -> set[str]:
    """fact_key of every fact this resume verifies (VERIFIED bullet under a VERIFIED role/project)."""
    from app.services.resume_verification import fact_key, verified_facts

    if resume is None:
        return set()
    return {fact_key(f["company"], f["claim"]) for f in verified_facts(resume.structured_data).values()}


def _links(db: Session, user_id: int, evidence_ids: list[int] | None = None) -> list[EvidenceResumeLink]:
    query = select(EvidenceResumeLink).where(EvidenceResumeLink.user_id == user_id)
    if evidence_ids is not None:
        if not evidence_ids:
            return []
        query = query.where(EvidenceResumeLink.evidence_id.in_(evidence_ids))
    return list(db.scalars(query).all())


def allowed_evidence_ids(db: Session, user_id: int, resume: Resume | None) -> set[int]:
    """Ids of the account's verified evidence a job using `resume` may draw on (the rule in the module docstring).
    With no resume at all, only untagged non-resume evidence qualifies."""
    rows = db.execute(select(Evidence.id, Evidence.source, Evidence.fact_key)
                      .where(Evidence.user_id == user_id, Evidence.verified.is_(True))).all()
    if not rows:
        return set()
    links: dict[int, list[EvidenceResumeLink]] = defaultdict(list)
    for link in _links(db, user_id):
        links[link.evidence_id].append(link)
    keys = verified_fact_keys(resume)
    resume_id = resume.id if resume is not None else None
    allowed: set[int] = set()
    for evidence_id, source, key in rows:
        own = links.get(evidence_id, [])
        mine = next((link for link in own if link.resume_id == resume_id), None)
        if mine is not None:
            if not mine.excluded:
                allowed.add(evidence_id)
            continue
        if source == "resume":
            if key and key in keys:
                allowed.add(evidence_id)
            continue
        if any(not link.excluded for link in own):
            continue  # tagged to other resumes only
        allowed.add(evidence_id)
    return allowed


# ───────────── automatic tags ─────────────

def sync_auto_links(db: Session, resume: Resume, evidence_ids: set[int]) -> None:
    """After a resume's facts were (re)verified: AUTO-link the evidence it verifies, and drop AUTO links of this resume
    to resume evidence it no longer verifies. USER links, a person's exclusions and the AUTO links of evidence
    extracted from the resume (not a verified fact of it) are left exactly as they are."""
    existing = {link.evidence_id: link for link in db.scalars(select(EvidenceResumeLink).where(
        EvidenceResumeLink.user_id == resume.user_id, EvidenceResumeLink.resume_id == resume.id)).all()}
    for evidence_id in evidence_ids:
        if evidence_id not in existing:
            db.add(EvidenceResumeLink(user_id=resume.user_id, evidence_id=evidence_id, resume_id=resume.id,
                                      origin=AUTO, excluded=False, created_at=now(), updated_at=now()))
    stale = [evidence_id for evidence_id, link in existing.items()
             if link.origin == AUTO and not link.excluded and evidence_id not in evidence_ids]
    if stale:
        from_resume = set(db.scalars(select(Evidence.id).where(
            Evidence.user_id == resume.user_id, Evidence.source == "resume", Evidence.id.in_(stale))).all())
        for evidence_id in from_resume:
            db.delete(existing[evidence_id])


def link_extracted(db: Session, resume: Resume | None, evidence: list[Evidence]) -> None:
    """Evidence extracted from a resume's text belongs to that resume (AUTO)."""
    if resume is None:
        return
    db.flush()  # ids
    for row in evidence:
        db.add(EvidenceResumeLink(user_id=resume.user_id, evidence_id=row.id, resume_id=resume.id, origin=AUTO,
                                  excluded=False, created_at=now(), updated_at=now()))


def backfill(db: Session, user_id: int) -> int:
    """AUTO links for resume evidence created before tags existed (idempotent; never touches a person's choice).
    Retrieval does not depend on it — the fact_key rule already scopes resume evidence — it makes the tags visible."""
    resumes = db.scalars(select(Resume).where(Resume.user_id == user_id)).all()
    evidence = db.execute(select(Evidence.id, Evidence.fact_key).where(
        Evidence.user_id == user_id, Evidence.source == "resume", Evidence.verified.is_(True))).all()
    by_key: dict[str, list[int]] = defaultdict(list)
    for evidence_id, key in evidence:
        if key:
            by_key[key].append(evidence_id)
    have = {(link.evidence_id, link.resume_id) for link in _links(db, user_id)}
    added = 0
    for resume in resumes:
        for key in verified_fact_keys(resume):
            for evidence_id in by_key.get(key, []):
                if (evidence_id, resume.id) not in have:
                    db.add(EvidenceResumeLink(user_id=user_id, evidence_id=evidence_id, resume_id=resume.id,
                                              origin=AUTO, excluded=False, created_at=now(), updated_at=now()))
                    have.add((evidence_id, resume.id))
                    added += 1
    if added:
        db.flush()
    return added


# ───────────── a person's tags ─────────────

def _owned_resumes(db: Session, user_id: int, resume_ids: list[int]) -> dict[int, Resume]:
    wanted = {int(r) for r in resume_ids}
    if not wanted:
        return {}
    found = {r.id: r for r in db.scalars(select(Resume).where(Resume.user_id == user_id,
                                                              Resume.id.in_(wanted))).all()}
    missing = wanted - set(found)
    if missing:
        raise TagError(f"Unknown resume(s): {', '.join(str(m) for m in sorted(missing))}")
    return found


def _evidence(db: Session, user_id: int, evidence_id: int) -> Evidence:
    row = db.scalar(select(Evidence).where(Evidence.user_id == user_id, Evidence.id == evidence_id))
    if row is None:
        raise TagError(f"Unknown evidence {evidence_id}")
    return row


def set_resumes(db: Session, user_id: int, evidence_id: int, resume_ids: list[int]) -> dict[str, Any]:
    """Make `resume_ids` exactly the resumes this evidence belongs to. For resume evidence, resumes it leaves keep a
    record of the person's removal when verification had linked them (`excluded`), so a later verification does not
    add them back; for other evidence the link is simply removed. An empty list makes manual or extracted evidence
    apply to every resume again; resume evidence then applies to none."""
    row = _evidence(db, user_id, evidence_id)
    wanted = _owned_resumes(db, user_id, resume_ids)
    links = {link.resume_id: link for link in _links(db, user_id, [evidence_id])}
    derived = _derived_resume_ids(db, user_id, [row]).get(row.id, set())
    for resume_id in wanted:
        link = links.get(resume_id)
        if link is None:
            db.add(EvidenceResumeLink(user_id=user_id, evidence_id=row.id, resume_id=resume_id, origin=USER,
                                      excluded=False, created_at=now(), updated_at=now()))
        elif link.excluded or link.origin != USER:
            link.excluded, link.origin, link.updated_at = False, USER, now()
    for resume_id in (set(links) | derived) - set(wanted):
        link = links.get(resume_id)
        if link is None:  # a fact_key match without a stored link yet: record the person's removal
            db.add(EvidenceResumeLink(user_id=user_id, evidence_id=row.id, resume_id=resume_id, origin=USER,
                                      excluded=True, created_at=now(), updated_at=now()))
        elif row.source != "resume" or (link.origin == USER and not link.excluded and resume_id not in derived):
            db.delete(link)  # nothing re-adds it: only a resume's verified facts are linked again automatically
        else:
            link.excluded, link.origin, link.updated_at = True, USER, now()
    db.flush()
    return describe(db, user_id, [row])[row.id]


def bulk(db: Session, user_id: int, evidence_ids: list[int], add: list[int], remove: list[int]) -> dict[str, Any]:
    """Add and remove resume tags on many evidence rows; each row is changed as set_resumes would."""
    _owned_resumes(db, user_id, [*add, *remove])
    rows = db.scalars(select(Evidence).where(Evidence.user_id == user_id,
                                             Evidence.id.in_([int(e) for e in evidence_ids]))).all()
    current = describe(db, user_id, rows)
    for row in rows:
        have = {r["id"] for r in current[row.id]["resumes"]}
        set_resumes(db, user_id, row.id, sorted((have | set(add)) - set(remove)))
    return {"updated": len(rows)}


def forget_resume(db: Session, resume_id: int) -> None:
    """A deleted resume's tags (the database cascade does this on PostgreSQL; SQLite needs it said)."""
    db.execute(delete(EvidenceResumeLink).where(EvidenceResumeLink.resume_id == resume_id))


# ───────────── reading ─────────────

def _derived_resume_ids(db: Session, user_id: int, rows: list[Evidence]) -> dict[int, set[int]]:
    """Resume evidence → the resumes that verify the same fact today (fact_key), whether or not a link is stored."""
    keyed = {r.id: r.fact_key for r in rows if r.source == "resume" and r.fact_key}
    if not keyed:
        return {}
    resumes = db.scalars(select(Resume).where(Resume.user_id == user_id)).all()
    keys_by_resume = {resume.id: verified_fact_keys(resume) for resume in resumes}
    return {evidence_id: {rid for rid, keys in keys_by_resume.items() if key in keys}
            for evidence_id, key in keyed.items()}


def describe(db: Session, user_id: int, rows: list[Evidence]) -> dict[int, dict[str, Any]]:
    """Per evidence id: {resumes: [{id, name, version, origin}], all_resumes} — what the Evidence Bank shows."""
    if not rows:
        return {}
    links: dict[int, dict[int, EvidenceResumeLink]] = defaultdict(dict)
    for link in _links(db, user_id, [r.id for r in rows]):
        links[link.evidence_id][link.resume_id] = link
    derived = _derived_resume_ids(db, user_id, rows)
    resumes = {r.id: r for r in db.scalars(select(Resume).where(Resume.user_id == user_id)).all()}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        effective: dict[int, str] = {}
        for resume_id in derived.get(row.id, set()):
            effective[resume_id] = AUTO
        for resume_id, link in links.get(row.id, {}).items():
            if link.excluded:
                effective.pop(resume_id, None)
            else:
                effective[resume_id] = link.origin
        tags = [{"id": rid, "name": resumes[rid].name or f"Resume v{resumes[rid].version}",
                 "version": resumes[rid].version, "origin": origin}
                for rid, origin in sorted(effective.items()) if rid in resumes]
        out[row.id] = {"resumes": tags, "all_resumes": not tags and row.source != "resume"}
    return out
