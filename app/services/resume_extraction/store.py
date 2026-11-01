"""[K] A resume version's extraction record (ResumeExtraction): written once when the file is read with V2, and read
by the parser and the review screen.

    save(db, resume, extraction)   the reader, the quality report, the section map and the parse view
    parse_text(db, resume)         what the regex parser reads: the parse view when the version has one, else its
                                   raw text exactly as before (every version read without V2)
    summary(db, resume_id)         for the screen: route and reasons, sections and how each heading was placed

No resume words beyond the document's own headings and the view; nothing a person typed. A version without a record
is "unknown", never "fine".
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Resume, ResumeExtraction
from app.services.resume_extraction.models import Extraction

PIPELINE_VERSION = "v2.3"  # bump when what a record holds, or how it is computed, changes


def save(db: Session, resume: Resume, extraction: Extraction) -> ResumeExtraction:
    """Record how this version was read (replacing an earlier record of the same version). Does not commit."""
    row = db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == resume.id))
    if row is None:
        row = ResumeExtraction(user_id=resume.user_id, resume_id=resume.id)
        db.add(row)
    row.pipeline_version = PIPELINE_VERSION
    row.source = extraction.layer.source if extraction.layer is not None else (
        "docx" if any(s.get("signal") == "style" for s in extraction.sections) else "text")
    row.quality = extraction.quality.summary()
    row.sections = list(extraction.sections)
    row.parse_view = extraction.view
    row.corrections, row.review = [], []
    db.flush()
    return row


def record(db: Session, resume_id: int) -> ResumeExtraction | None:
    return db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == resume_id))


def parse_text(db: Session, resume: Resume) -> str:
    """The text the regex parser reads for this version."""
    row = record(db, resume.id) if resume.id is not None else None
    return row.parse_view if row is not None and row.parse_view else (resume.raw_text or "")


def summary(db: Session, resume_id: int) -> dict[str, Any] | None:
    row = record(db, resume_id)
    if row is None:
        return None
    return {"pipeline_version": row.pipeline_version, "source": row.source, "quality": row.quality,
            "sections": [{k: s.get(k) for k in ("text", "kind", "method", "signal", "line")} for s in row.sections]}
