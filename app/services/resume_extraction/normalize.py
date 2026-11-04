"""[A] Normalisation of extracted text: characters that are artefacts of extraction, and words a line break split.

Deliberately narrower than NFKC. NFKC also rewrites characters that carry meaning ("m²" → "m2", "½" → "1\u20442",
"™" → "TM", "①" → "1"), and a wrong change is worse than a missed one. Only forms that are pure artefacts change:

    ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ              → their letters (Unicode's ligature presentation forms)
    Ａ–ｚ, ０–９ and full-width punctuation → ASCII
    no-break and other spaces   → a space;  zero-width characters and soft hyphens (U+00AD) → removed
    PDFium's hyphen marker (U+FFFE, U+0002) → "-"

A word split across a line break is joined only on evidence (`join_lines`): "optimiza-" / "tion" becomes
"optimization" because that is a known word and "optimiza" is not; "real-" / "time" stays "real-time", since both
halves are words; an unknown pair ("Kuber-" / "netes") keeps its hyphen. Either way the line break goes, because the
word continues on the next line.
"""
from __future__ import annotations

import re
import unicodedata

from app.services.text_repair import known

_PRESENTATION = {chr(c): unicodedata.normalize("NFKC", chr(c)) for c in range(0xFB00, 0xFB07)}
_FULLWIDTH = {chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)}
_SPACES = dict.fromkeys(["\xa0", "\u2007", "\u202f", "\u2002", "\u2003", "\u2009", "\u200a", "\u3000"], " ")
_GONE = dict.fromkeys(["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\xad"], "")
_HYPHEN = {"\ufffe": "-", "\x02": "-"}
TABLE = str.maketrans({**_PRESENTATION, **_FULLWIDTH, **_SPACES, **_GONE, **_HYPHEN})

# "<letters>-<line break><lower-case letters>": a word that may have been split by the line break.
_SPLIT = re.compile(r"([A-Za-z][A-Za-z']*)-[ \t]*\n[ \t]*([a-z][A-Za-z']*)")


def normalize_chars(text: str) -> str:
    return (text or "").translate(TABLE)


def _joined(match: re.Match[str]) -> str:
    head, tail = match.group(1), match.group(2)
    if known(head + tail) and not (known(head) and known(tail)):
        return head + tail
    return f"{head}-{tail}"


def join_lines(text: str) -> str:
    return _SPLIT.sub(_joined, text or "")


def normalize(text: str) -> str:
    return join_lines(normalize_chars(text))
