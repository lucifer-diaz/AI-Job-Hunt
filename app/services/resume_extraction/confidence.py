"""[I] How far each fact of a resume read with V2 can be trusted, from signals code can check. Never a model's word.

    high     every value is in the file's text word for word, and nothing in it was corrected (or the page and a
             rule agree on the correction, or the person kept it)
    medium   a correction backed by one kind of evidence plus the dictionary (the page, or a consistent rule); a skill
             normalized from its written form ("Apache Kafka" from "Kafka"), or kept through a known alias; a bullet
             that matches the text except for a few words; a word no dictionary knows in a file whose text may be
             misread
    low      a value not found in the text as written, or a word that still looks misread
    and      a file whose text layer was poor lowers every fact one level

Computed when the resume is read (`assess`), from the structured data, the extraction record and the stored text,
so it never goes stale when a correction is undone or a heading reassigned. A version read without V2 has no
confidence (unknown, never "high"). Confidence orders and speeds review; it never verifies a fact by itself: only a
person's "accept" does (resume_verification.verify_confident).
"""
from __future__ import annotations

import re
from typing import Any

from app.services import structured_resume as sr

LEVELS = ("low", "medium", "high")
# Normalized or derived fields (a YYYY-MM date, a category label): not words of the text.
_DERIVED = {"start_date", "end_date", "is_current", "category", "raw", "employment_type"}
_PASSAGES = {"text", "description"}  # sentences: a few broken words may be tolerated, at medium


def _cap(level: str, at_most: str) -> str:
    return LEVELS[min(LEVELS.index(level), LEVELS.index(at_most))]


def _values(unit: sr.Unit) -> list[tuple[str, str]]:
    """(field, value) for every textual value of a unit."""
    out = []
    for key, value in unit.content().items():
        if key in _DERIVED or value in (None, "", [], False):
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item.strip():
                out.append((key, item.strip()))
    return out


def assess(data: dict[str, Any] | None, raw_text: str, row) -> dict[str, dict[str, Any]]:
    """{item_id: {"level", "reasons"}} for every unit, or {} for a version read without V2."""
    from app.services.resume_extraction import quality, store
    from app.services.resume_parsing import Source, _norm, normalizes

    if row is None or not data:
        return {}
    clean = store.clean_text(row, raw_text)
    src = Source(clean)
    fixes = {_norm(c["corrected"]): c for c in row.corrections or [] if c.get("status") != "undone"}
    route = (row.quality or {}).get("route", "clean")
    out: dict[str, dict[str, Any]] = {}
    for unit in sr.iter_units(data):
        level, reasons = "high", []
        values = _values(unit)
        words = {w for _, v in values for w in re.findall(r"[^\s,;:/()|]+", v)}
        for word in words:
            fix = fixes.get(_norm(word))
            if fix is None:
                continue
            what = f"corrected “{fix['original']}” → “{fix['corrected']}”"
            if fix.get("status") == "kept":
                reasons.append(f"{what}, which you kept")
            elif fix.get("method") == "ocr+rule":
                reasons.append(f"{what}: the page and a rule agree")
            else:
                level = _cap(level, "medium")
                reasons.append(f"{what}: {'the page shows it' if fix.get('method') == 'ocr' else 'a consistent substitution'}")
        for field, value in values:
            if src.has(value):
                continue
            if unit.kind == "skills" and unit.node.get("raw") and normalizes(unit.node["raw"], value):
                level = _cap(level, "medium")
                reasons.append(f"normalized from “{unit.node['raw']}”")
            elif unit.kind == "skills" and src.has_skill(value):
                level = _cap(level, "medium")
                reasons.append("found through a known alias")
            elif field in _PASSAGES and src.has_passage(value):
                level = _cap(level, "medium")
                reasons.append("matches the text except for a few words")
            else:
                level = _cap(level, "low")
                reasons.append(f"“{value[:60]}” is not in the text as written")
        if route != "clean":
            odd = sorted({w.strip(".!?'’") for w in words if quality.broken(w.strip(".!?'’"))})
            unknown = sorted({w for w in (w.strip(".!?'’") for w in words) if w.isalpha() and len(w) >= 4
                              and not quality.familiar(w)} - set(odd))
            if odd:
                level = _cap(level, "low")
                reasons.append(f"looks misread: {', '.join(odd[:3])}")
            elif unknown and unit.kind in ("skills", "bullet", "summary"):
                level = _cap(level, "medium")
                reasons.append(f"a word no dictionary knows, in a file that may be misread: {', '.join(unknown[:3])}")
        if route in ("poor", "no_text"):
            level = LEVELS[max(0, LEVELS.index(level) - 1)]
            reasons.append("the file's text was poor")
        out[unit.item_id] = {"level": level, "reasons": reasons or ["in the file's text word for word"]}
    return out
