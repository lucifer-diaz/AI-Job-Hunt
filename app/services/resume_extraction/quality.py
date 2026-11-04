"""[D] How far a document's text layer can be trusted, from checks code can make. No model, no OCR.

    no_text      (almost) no text at all: a scanned PDF; only OCR can read it
    poor         many unknown lower-case words, broken-glyph suspects or bad characters: read it again (OCR)
    suspicious   a few: check the suspicious words (OCR of crops, Phase 6)
    clean        nothing to check

Unknown words are counted among lower-case words only: names, employers and products are capitalised and would
drown the signal. A word a broken glyph turned into another real-looking word ("Kaga") is invisible here; that is
what OCR agreement is for. The report holds counts and the route, never words.
"""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from app.services.resume_extraction.models import PageQuality, QualityReport, TextLayer
from app.services.text_repair import known, suspect

# Tuned on the resume_extraction fixtures (docs/evals/): every clean document routes "clean", every document with
# broken mappings does not.
MIN_CHARS_PER_PAGE = 80
SUSPICIOUS_OOV, POOR_OOV = 0.03, 0.10
POOR_SUSPECT_SHARE = 0.02  # broken-glyph suspects among all words
POOR_BAD_CHAR_SHARE = 0.005
BAD_FLAGS = ("replacement_char", "private_use", "cid_code", "unmapped_glyph")  # information lost, not just odd

_WORD = re.compile(r"[^\s|\u2022·,;:/()\"\u201c\u201d]+")
_CAMEL = re.compile(r"[A-Z][a-z]*|[a-z]+")


@cache
def _vocabulary() -> frozenset[str]:
    path = Path(__file__).parent / "lexicons" / "vocabulary.txt"
    return frozenset(line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#"))


def familiar(word: str) -> bool:
    """A known English word, or a word resumes use that the dictionary lacks."""
    return word.lower() in _vocabulary() or known(word)


def _odd_capitals(part: str) -> bool:
    """A product name's capitals split it into a short or known head and known (or one- or two-letter) parts:
    "BigQuery", "PyTorch", "GitHub", "eBay", "PoCs". A broken glyph's do not: "PlaBorm", "aCenYon"."""
    parts = _CAMEL.findall(part)
    if "".join(parts) != part or len(parts) < 2:
        return True
    head, *rest = parts
    return not ((len(head) <= 3 or familiar(head)) and all(len(p) <= 2 or familiar(p) for p in rest))


def broken(word: str) -> bool:
    """A word with a character that looks like a broken glyph ("architecYng", "pla[orms", "real-Yme")."""
    if sum(c.isdigit() for c in word) >= 2:
        return False  # an identifier (a link's id, a version), not a word
    parts = word.split("-")
    for i, part in enumerate(parts):
        if not part:
            continue
        after_lower = i > 0 and parts[i - 1].isalpha() and parts[i - 1].islower()
        if after_lower and part[0].isupper() and part[1:].islower() and not familiar(part):
            return True  # "real-Yme"; a Title-Case compound ("Event-Driven") has a capital before the hyphen too
        if suspect(part) and _odd_capitals(part):
            return True
    return False


def _words(text: str) -> list[str]:
    return [w.strip(".'\u2019!?") for w in _WORD.findall(text or "") if w.strip(".'\u2019!?")]


def _page_quality(number: int, text: str) -> PageQuality:
    words = _words(text)
    lower = [w for w in words if w.isalpha() and w.islower() and len(w) >= 3]
    return PageQuality(number=number, chars=sum(not c.isspace() for c in text), words=len(words),
                       lowercase_words=len(lower), oov_words=sum(not familiar(w) for w in lower),
                       suspect_words=sum(broken(w) for w in words))


def assess(text: str, flags: dict[str, int], layer: TextLayer | None = None) -> QualityReport:
    """The report for a text layer (`text` as the file states it, before repair)."""
    if layer is not None:
        pages = [_page_quality(p.number, "\n".join(line.text for line in p.lines)) for p in layer.pages]
    else:
        pages = [_page_quality(1, text)]
    chars, words = sum(p.chars for p in pages), sum(p.words for p in pages)
    lower, oov = sum(p.lowercase_words for p in pages), sum(p.oov_words for p in pages)
    suspects = sum(p.suspect_words for p in pages)
    bad = sum(flags.get(k, 0) for k in BAD_FLAGS)
    oov_rate = round(oov / lower, 4) if lower else None
    reasons: list[str] = []
    empty = [p.number for p in pages if p.chars < MIN_CHARS_PER_PAGE]
    if not pages or len(empty) == len(pages):
        return QualityReport("no_text", ["no text layer: a scanned document"], chars, words, oov_rate, suspects,
                             flags, pages)
    if empty:
        reasons.append(f"pages without text: {', '.join(map(str, empty))}")
    if oov_rate is not None and oov_rate >= SUSPICIOUS_OOV:
        reasons.append(f"{oov} of {lower} lower-case words unknown ({oov_rate:.1%})")
    if suspects:
        reasons.append(f"{suspects} words with a character that looks like a broken glyph")
    if bad:
        reasons.append(f"{bad} characters the file could not map")
    poor = ((oov_rate or 0) >= POOR_OOV or suspects >= max(2, POOR_SUSPECT_SHARE * words)
            or bad >= max(1, POOR_BAD_CHAR_SHARE * chars))
    route = "poor" if poor else "suspicious" if reasons else "clean"
    return QualityReport(route, reasons, chars, words, oov_rate, suspects, flags, pages)
