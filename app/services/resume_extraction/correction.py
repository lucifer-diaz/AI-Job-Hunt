"""[E] Corrections of words a PDF's text layer got wrong, each backed by evidence and recorded. Deterministic.

A wrong correction is worse than a missed one, so:

    only unknown words    a word the dictionary or the resume vocabulary knows is never changed ("Python",
                          "Hybrid", "Analytics" stay, whatever a rule says about "y")
    ocr                   the page shows a known word at the same place, close to the layer's (at most two glyphs
                          apart, each read as up to three letters): "Kaga" where the page reads "Kafka"
    rule                  a substitution this file uses consistently: from the seed table (lexicons/confusions.yaml)
                          or from the OCR disagreements, accepted only when it turns at least MIN_RULE_WORDS different
                          unknown words into known ones, and applied only to words it makes known
    neither               the word stays as written (still counted as suspicious in the quality report)

Every change is a Correction (original → corrected, method, evidence, how often), stored on the extraction record and
shown to the person, who can undo it. The resume's raw text is never rewritten: `apply` derives the clean text.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path

from app.services.resume_extraction.quality import broken, familiar

MIN_RULE_WORDS = 2
MAX_OCR_EDIT = 2  # glyphs the page's reading may differ by
MAX_GLYPH_LETTERS = 3  # letters one glyph may stand for (a ligature: "ffi")
_WORD = re.compile(r"[^\s|•·,;:/()\"“”]+")
_EDGE = ".!?'’"


@dataclass
class Correction:
    original: str
    corrected: str
    method: str  # ocr | rule | ocr+rule
    evidence: list[str] = field(default_factory=list)
    count: int = 0
    status: str = "applied"  # applied | kept (the person confirmed) | undone (the person rejected)

    @property
    def id(self) -> str:
        from app.services.structured_resume import short_hash

        return short_hash(self.original, self.corrected)

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "id": self.id}


@cache
def confusions() -> dict[str, tuple[str, ...]]:
    import yaml

    path = Path(__file__).parent / "lexicons" / "confusions.yaml"
    return {str(k): tuple(v) for k, v in (yaml.safe_load(path.read_text()) or {}).items()}


def _strip(word: str) -> str:
    return word.strip(_EDGE)


def _tokens(text: str) -> list[str]:
    return [t for t in (_strip(w) for w in _WORD.findall(text or "")) if t]


def candidate(word: str) -> bool:
    """A word a correction may touch: unknown (or broken-looking), with letters, not an identifier."""
    if len(word) < 3 or sum(c.isdigit() for c in word) >= 2 or not any(c.isalpha() for c in word):
        return False
    return broken(word) or (not familiar(word) and word.isascii())


def _substitute(word: str, glyph: str, seq: str) -> str | None:
    """The word with `glyph` read as `seq` (every occurrence, else one at a time) when that makes a known word."""
    if glyph not in word:
        return None
    every = word.replace(glyph, seq)
    if familiar(every):
        return every
    for i, ch in enumerate(word):
        if ch == glyph and familiar(one := word[:i] + seq + word[i + 1:]):
            return one
    return None


def _glyph_edits(word: str, reading: str) -> int:
    """How many glyphs of the layer's word the page reads differently. A glyph may stand for up to MAX_GLYPH_LETTERS
    letters (a ligature drawn as one: "C" for "tt", "g" for "fk"), so "aCribuYon" → "attribution" is two, while
    "prxgrxmmxng" → "programming" is three. A difference that spans more than one glyph of the layer counts as
    too many."""
    count = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, word, reading, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i2 - i1 > 1 or j2 - j1 > MAX_GLYPH_LETTERS:
            return MAX_OCR_EDIT + 1
        count += 1
    return count


def _close(word: str, reading: str) -> bool:
    """At most MAX_OCR_EDIT glyphs apart (`_glyph_edits`), the rest of the word the same."""
    return _glyph_edits(word.lower(), reading.lower()) <= MAX_OCR_EDIT


def _rule_from(word: str, reading: str) -> tuple[str, str] | None:
    """The one-glyph substitution that turns the layer's word into the page's ("g" → "fk" for Kaga → Kafka)."""
    ops = [op for op in SequenceMatcher(None, word, reading, autojunk=False).get_opcodes() if op[0] != "equal"]
    if len(ops) == 1 and ops[0][0] == "replace" and ops[0][2] - ops[0][1] == 1:
        _, i1, i2, j1, j2 = ops[0]
        return word[i1:i2], reading[j1:j2]
    return None


def correct(text: str, readings: list[tuple[str, str]] | None = None) -> list[Correction]:
    """The corrections this text warrants, from the page's readings (align.readings) and consistent rules."""
    words = Counter(_tokens(text))
    suspects = [w for w in words if candidate(w)]
    seen: dict[str, Counter[str]] = {}
    from app.services.resume_extraction.normalize import normalize_chars

    for layer_word, page_word in readings or []:  # the layer's word as the text has it after normalisation (ﬁ → fi)
        for lw, pw in zip(_tokens(normalize_chars(layer_word)), _tokens(normalize_chars(page_word)), strict=False):
            if lw in words:
                seen.setdefault(lw, Counter())[pw] += 1
    out: dict[str, Correction] = {}
    # The page's reading.
    inferred: Counter[tuple[str, str]] = Counter()
    for word in suspects:
        if word not in seen:
            continue
        reading = seen[word].most_common(1)[0][0]
        if reading != word and familiar(reading) and _close(word, reading):
            out[word] = Correction(word, reading, "ocr", [f"the page reads “{reading}” there"], words[word])
            if rule := _rule_from(word, reading):
                inferred[rule] += 1
    # Consistent substitutions: the seed table and what the page's readings showed.
    rules = {(g, s) for g, seqs in confusions().items() for s in seqs} | set(inferred)
    for glyph, seq in sorted(rules):
        fixed = {w: f for w in suspects if (f := _substitute(w, glyph, seq)) is not None}
        if len(fixed) < MIN_RULE_WORDS:
            continue
        note = (f"“{glyph}” stands for “{seq}” in this file: it makes {len(fixed)} unknown words known "
                f"({', '.join(sorted(fixed)[:3])}{'…' if len(fixed) > 3 else ''})")
        for word, fix in fixed.items():
            if word in out:
                if out[word].corrected == fix and out[word].method == "ocr":
                    out[word].method, out[word].evidence = "ocr+rule", [*out[word].evidence, note]
                continue
            out[word] = Correction(word, fix, "rule", [note], words[word])
    return sorted(out.values(), key=lambda c: (-c.count, c.original))


def _pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![^\s|•·,;:/()\"“”]){re.escape(word)}(?=[\s|•·,;:/()\"“”.!?'’]|$)")


def apply(text: str, corrections: list[dict[str, object]] | list[Correction]) -> str:
    """The text with every correction not undone applied, word for word. Line structure is unchanged."""
    out = text or ""
    for c in corrections:
        c = c.as_dict() if isinstance(c, Correction) else c
        if c.get("status") != "undone":
            out = _pattern(str(c["original"])).sub(str(c["corrected"]), out)
    return out
