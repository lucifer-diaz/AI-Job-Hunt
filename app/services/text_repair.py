"""Repair ligature glyphs a PDF's font mapped to the wrong character. Deterministic, no model.

Some PDF exports draw "ti", "tf", "fi" … as one glyph and give it a broken Unicode mapping, so text extraction reads
"architecting" as "architecYng" and "platforms" as "pla[orms". Every parser downstream (regex and AI, whose output is
grounded in this text) then keeps the misspelling. A font maps a glyph the same way everywhere, so:

    suspects     words where one odd character sits inside lower-case letters ("architecYng", "pla[orms",
                 "mulY-tenant", "real-Yme")
    one mapping  per odd character: the ligature that turns the most of its suspects into known English words, and
                 clearly more than any other ligature does
    replace      a suspect only when the mapping makes it a known word, symbols included: "pla[orms" becomes
                 "platforms", while "arr[index]" in the same text stays as written

A word that is already a known word, and an odd character no ligature explains ("eBay", "JavaScript", "k8s"), are
left exactly as they are. Clean text comes back byte-identical. A glyph that came out as an ordinary lower-case
letter cannot be told from a real one and is not repaired.
"""
from __future__ import annotations

import gzip
import re
from collections import Counter
from functools import cache
from pathlib import Path

LIGATURES = ("ti", "tf", "ft", "tt", "fi", "fl", "ff", "ffi", "ffl", "fj", "th", "st", "ct")
# Characters that sit inside real words or tokens ("C++", "e-mail", "don't", "k8s", "AT&T"): never glyphs.
_ORDINARY = set("-'’./&+#_@:,") | set("0123456789")
# A run of letters and odd characters; odd ones are checked against the context below.
_TOKEN = re.compile(r"[^\s\-/,.;:()\"“”]+(?:-[^\s\-/,.;:()\"“”]+)*")
MIN_HITS = 2  # a mapping needs at least two repaired occurrences (one, for a symbol only one ligature explains)
MIN_SHARE = 0.5  # and must explain at least half of its glyph's suspects

# Common modern words the 1913 / 1934 dictionaries behind the bundled list do not have.
MODERN = frozenset("""
expertise scalability scalable containerization containerized microservice microservices workflow workflows
dataset datasets pipeline pipelines analytics stakeholder stakeholders onboarding offboarding backend frontend
fullstack devops deployment deployments automate automated automation optimization optimizations monetization
prioritization personalization customization visualization virtualization multitenant multitenancy realtime
online offline website websites email emails startup startups fintech healthtech edtech proactive proactively
ecommerce chatbot chatbots metadata middleware firmware spreadsheet spreadsheets troubleshoot troubleshooting
""".split())  # noqa: SIM905  (a word list reads best as words)


@cache
def _lexicon() -> frozenset[str]:
    path = Path(__file__).parent / "data" / "english_words.txt.gz"
    with gzip.open(path, "rt", encoding="ascii") as f:
        return frozenset(line.strip() for line in f if line.strip()) | MODERN


def _stems(word: str) -> list[str]:
    """The word and the forms an inflection or derivation would come from ("architecting" → "architect")."""
    out = [word]
    rules = (("ies", "y"), ("ied", "y"), ("ities", "ity"), ("ization", "ize"), ("izations", "ize"), ("ation", "ate"),
             ("ations", "ate"), ("ation", "e"), ("ation", ""), ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"),
             ("es", ""), ("s", ""), ("er", ""), ("ers", ""), ("ly", ""), ("ally", ""), ("ness", ""), ("ment", ""),
             ("ments", ""), ("al", ""), ("ive", "e"), ("ive", ""))
    for suffix, replacement in rules:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: -len(suffix)] + replacement
            out.append(stem)
            if suffix in ("ing", "ed", "er", "ers") and len(stem) > 3 and stem[-1] == stem[-2]:
                out.append(stem[:-1])  # "running" → "run"
    return out


def known(word: str) -> bool:
    """A known English word (or an inflection of one); a hyphenated word when every part is."""
    word = word.lower().strip("'’")
    if not word:
        return False
    if "-" in word:
        return all(known(part) for part in word.split("-") if part)
    if not word.isalpha():
        return False
    lexicon = _lexicon()
    return any(stem in lexicon for stem in _stems(word))


def _odd(ch: str) -> bool:
    return not ("a" <= ch <= "z") and ch not in _ORDINARY and not ch.isspace()


def _suspects(token: str) -> list[int]:
    """Positions of characters in a token that look like a broken glyph: inside lower-case letters, or ending a
    lower-case stem, or starting a lower-case part after a hyphen ("real-Yme")."""
    out = []
    for i, ch in enumerate(token):
        if not _odd(ch):
            continue
        before = token[i - 1] if i else ""
        after = token[i + 1] if i + 1 < len(token) else ""
        lower_before, lower_after = before.islower() and before.isascii(), after.islower() and after.isascii()
        hyphen_part = before == "-" and i >= 2 and token[i - 2].islower() and lower_after
        if (lower_before and lower_after) or hyphen_part or (lower_before and after in ("", "-") and i >= 3):
            out.append(i)
    return out


def _with(token: str, glyph: str, ligature: str) -> str:
    out, positions = token, [i for i in _suspects(token) if token[i] == glyph]
    for i in reversed(positions):
        out = out[:i] + ligature + out[i + 1:]
    return out


def mappings(text: str) -> dict[str, str]:
    """Each broken glyph in the text and the ligature it stands for. Empty for clean text."""
    tokens = Counter(t for t in _TOKEN.findall(text or "") if _suspects(t))
    if not tokens:
        return {}
    by_glyph: dict[str, Counter[str]] = {}
    for token, n in tokens.items():
        for i in _suspects(token):
            by_glyph.setdefault(token[i], Counter())[token] += n
    out = {}
    for glyph, suspects in by_glyph.items():
        total = sum(suspects.values())
        scores = []
        for ligature in LIGATURES:
            hits = sum(n for token, n in suspects.items()
                       if not known(token) and known(_with(token, glyph, ligature)))
            scores.append((hits, ligature))
        scores.sort(reverse=True)
        (best, ligature), runner_up = scores[0], scores[1][0]
        symbol = not glyph.isalpha()
        enough = best >= MIN_HITS or (best == 1 and symbol and runner_up == 0)
        if enough and best > runner_up and best >= MIN_SHARE * total:
            out[glyph] = ligature
    return out


def repair_glyphs(text: str) -> str:
    """The text with broken ligature glyphs restored. Byte-identical when nothing is repaired."""
    found = mappings(text)
    if not found:
        return text

    def fix(match: re.Match[str]) -> str:
        token = match.group(0)
        positions = [i for i in _suspects(token) if token[i] in found]
        if not positions or known(token):
            return token
        repaired = token
        for i in reversed(positions):
            repaired = repaired[:i] + found[token[i]] + repaired[i + 1:]
        return repaired if known(repaired) else token

    return _TOKEN.sub(fix, text)
