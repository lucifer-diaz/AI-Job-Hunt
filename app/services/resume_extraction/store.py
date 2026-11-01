"""[K] A resume version's extraction record (ResumeExtraction): written once when the file is read with V2, read by
the parser and the review screen, and changed only by a person's section choices.

    save(db, resume, extraction)    the reader, the quality report, the section map, each line's look, the parse view
    parse_text(db, resume)          what the regex parser reads: the parse view when the version has one, else its
                                    raw text exactly as before (every version read without V2)
    assign(db, resume, line, kind)  a person's choice for a heading (a section, or "none"): recorded as feedback, the
                                    view rebuilt from the stored text and look, the data re-parsed (filled while
                                    nobody has reviewed or edited it, else a comparison the person resolves)
    learned(db, user_id)            the account's own heading choices, counted, for its next uploads
    unplaced(db, resume_id)         headings no table could place and nobody has assigned: the resume cannot be
                                    verified until they are

No resume words beyond the document's own headings, a heading's first line as a preview and the view; nothing a
person typed. A version without a record is "unknown", never "fine".
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Resume, ResumeExtraction, ResumeExtractionFeedback
from app.services.resume_extraction import sections, structure
from app.services.resume_extraction.models import Extraction, LineInfo

PIPELINE_VERSION = "v2.4"  # bump when what a record holds, or how it is computed, changes
PREVIEW_CHARS = 90


class UnknownHeading(ValueError):
    pass


# ───────────── writing ─────────────

def _compact(info: LineInfo) -> dict[str, Any]:
    out = {"s": info.size, "b": info.bold or None, "x": info.x0, "st": info.style, "u": info.bullet or None,
           "g": info.segments}
    return {k: v for k, v in out.items() if v is not None}


def save(db: Session, resume: Resume, extraction: Extraction) -> ResumeExtraction:
    """Record how this version was read (replacing an earlier record of the same version). Does not commit."""
    row = record(db, resume.id)
    if row is None:
        row = ResumeExtraction(user_id=resume.user_id, resume_id=resume.id)
        db.add(row)
    row.pipeline_version = PIPELINE_VERSION
    row.source = extraction.layer.source if extraction.layer is not None else (
        "docx" if any(s.get("signal") == "style" for s in extraction.sections) else "text")
    row.quality = extraction.quality.summary()
    row.sections = list(extraction.sections)
    row.layout = [_compact(info) for info in extraction.lines] or None
    row.overrides = {}
    row.parse_view = extraction.view
    row.corrections, row.review = [], []
    db.flush()
    return row


# ───────────── reading ─────────────

def record(db: Session, resume_id: int) -> ResumeExtraction | None:
    return db.scalar(select(ResumeExtraction).where(ResumeExtraction.resume_id == resume_id))


def parse_text(db: Session, resume: Resume) -> str:
    """The text the regex parser reads for this version."""
    row = record(db, resume.id) if resume.id is not None else None
    return row.parse_view if row is not None and row.parse_view else (resume.raw_text or "")


def parse(db: Session, resume: Resume) -> dict[str, Any]:
    """The regex parse of this version: of its parse view (structure already marked) when it has one, else of its
    raw text exactly as before."""
    from app.services import structured_resume as sr

    row = record(db, resume.id) if resume.id is not None else None
    if row is not None and row.parse_view:
        return sr.parse_deterministic(row.parse_view, marked=True)
    return sr.parse_deterministic(resume.raw_text or "")


def lines(row: ResumeExtraction, raw_text: str) -> list[LineInfo]:
    """The stored text's lines with the look recorded for them (none when the record has no layout)."""
    texts = (raw_text or "").split("\n")
    layout = row.layout or []
    out = []
    for i, text in enumerate(texts):
        look = layout[i] if i < len(layout) and len(layout) == len(texts) else {}
        out.append(LineInfo(text=text, size=look.get("s"), bold=bool(look.get("b")), x0=look.get("x"),
                            style=look.get("st"), bullet=bool(look.get("u")), segments=look.get("g")))
    return out


def _preview(texts: list[str], line: int) -> str:
    following = next((t.strip() for t in texts[line + 1:] if t.strip()), "")
    return following if len(following) <= PREVIEW_CHARS else following[:PREVIEW_CHARS - 1].rstrip() + "…"


def unplaced(db: Session, resume_id: int) -> list[dict[str, Any]]:
    """Headings the table could not place and nobody has assigned yet."""
    row = record(db, resume_id)
    return [s for s in (row.sections if row is not None else []) if s.get("method") == "unknown"]


def summary(db: Session, resume_id: int) -> dict[str, Any] | None:
    row = record(db, resume_id)
    if row is None:
        return None
    resume = db.get(Resume, resume_id)
    texts = (resume.raw_text or "").split("\n") if resume is not None else []
    return {"pipeline_version": row.pipeline_version, "source": row.source, "quality": row.quality,
            "sections": [{**{k: s.get(k) for k in ("text", "kind", "method", "signal", "line")},
                          "preview": _preview(texts, int(s.get("line") or 0))} for s in row.sections],
            "unplaced": sum(s.get("method") == "unknown" for s in row.sections),
            "choices": [{"kind": k, "label": sections.CHOICE_LABELS[k]} for k in sections.CHOICES]}


# ───────────── a person's choices ─────────────

def learned(db: Session, user_id: int) -> dict[str, str]:
    """The account's own choices for headings, by how the heading is written: the section chosen most often (the
    latest on a tie). Counting, never inference; only this account's choices."""
    rows = db.scalars(select(ResumeExtractionFeedback).where(ResumeExtractionFeedback.user_id == user_id,
                                                             ResumeExtractionFeedback.kind == "section")
                      .order_by(ResumeExtractionFeedback.id)).all()
    counts: dict[str, Counter[str]] = {}
    latest: dict[tuple[str, str], int] = {}
    for row in rows:
        kind = (row.after or {}).get("kind")
        if kind not in sections.CHOICES:
            continue
        key = sections.phrase(row.subject)
        counts.setdefault(key, Counter())[kind] += 1
        latest[(key, kind)] = row.id
    return {key: max(c, key=lambda k, key=key: (c[k], latest[(key, k)])) for key, c in counts.items()}


def assign(db: Session, resume: Resume, line: int, kind: str) -> dict[str, Any]:
    """A person puts the heading on `line` in section `kind` ("none": it is not a heading). Does not commit.

    Returns {"filled": bool} — True when the data was re-parsed in place (nobody had reviewed or edited it), False
    when the new parse waits as a comparison."""
    from app.services import resume_parsing
    from app.services import resume_verification as rv
    from app.services import structured_resume as sr

    if kind not in sections.CHOICES:
        raise UnknownHeading(f"Unknown section '{kind}'")
    row = record(db, resume.id)
    infos = lines(row, resume.raw_text) if row is not None else []
    if row is None or not 0 <= line < len(infos) or not infos[line].text.strip():
        raise UnknownHeading("That line is not part of this resume")
    before = next((s for s in row.sections if s.get("line") == line), None)
    row.overrides = {**(row.overrides or {}), str(line): kind}
    db.add(ResumeExtractionFeedback(user_id=resume.user_id, resume_id=resume.id, kind="section",
                                    subject=infos[line].text.strip()[:300],
                                    before={"kind": before.get("kind"), "method": before.get("method")} if before else None,
                                    after={"kind": kind}))
    headings = sections.detect(infos, learned(db, resume.user_id), {int(k): v for k, v in row.overrides.items()})
    row.sections = [h.as_dict() for h in headings]
    row.parse_view = structure.view(infos, headings)
    db.flush()
    parsed = sr.parse_deterministic(row.parse_view, marked=True)
    if resume_parsing.untouched(db, resume, resume.revision or 0):
        resume.structured_data = sr.materialize(parsed)
        resume.pending_reparse = None
        resume.parser, resume.parser_version = "deterministic", sr.PARSER_VERSION
        rv.record(db, resume, "*", "PARSE", "PARSER", new={"parser": resume.parser, "sections": "assigned"})
        db.flush()
        return {"filled": True}
    rv.compare(db, resume, parsed, parser="deterministic", parser_version=sr.PARSER_VERSION)
    return {"filled": False}
