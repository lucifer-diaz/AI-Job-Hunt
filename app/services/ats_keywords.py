"""ATS keyword coverage: which of the posting's keywords a tailored resume contains. Deterministic, no model.

    keywords  the parsed posting's required skills, then its preferred skills, then its keyword list
              (job.structured_description); else the posting's skills; else, for older jobs, the ranking's lists
    covered   the keyword appears in the resume as a whole word or phrase ("Python", "CI/CD", "Node.js")
    partial   most of a multi-word keyword's words appear ("distributed systems" → "distributed ... systems"),
              or the ranking lists it as a gap the resume only partly covers
    missing   neither

The score is covered + half of partial, over the keywords shown. Ranking requirements are sentences ("5+ years
building distributed systems in Python"), which no resume contains word for word: they are only the last resort.
"""
from __future__ import annotations

import re
from itertools import pairwise
from typing import Any

MAX_KEYWORDS = 20
# Words that carry no keyword meaning on their own ("experience with", "strong").
STOPWORDS = {"and", "or", "of", "the", "a", "an", "in", "on", "for", "with", "to", "experience", "strong", "skills",
             "knowledge", "proficiency", "using", "years", "year", "plus", "etc", "e.g", "good", "solid", "deep",
             "hands-on", "working", "understanding", "familiarity", "ability"}


def normalize(text: str) -> str:
    """Lower case; dots, slashes, hyphens and underscores read as spaces, so "Node.js" matches "nodejs" / "node js"
    and "CI/CD" matches "ci cd". "+" and "#" stay ("C++", "C#")."""
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[./\\_\-]+", " ", text)
    text = re.sub(r"[^\w+# ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    if re.search(rf"(?<![\w+#]){re.escape(needle)}(?![\w+#])", haystack):
        return True
    joined = needle.replace(" ", "")  # "node js" also matches "nodejs"
    if joined != needle and re.search(rf"(?<![\w+#]){re.escape(joined)}(?![\w+#])", haystack):
        return True
    if " " not in needle:  # "nodejs" also matches "Node.js" (normalized to "node js"): two adjacent words, joined
        words = haystack.split()
        return any(a + b == needle for a, b in pairwise(words))
    return False


def _significant(words: str) -> list[str]:
    return [w for w in words.split() if w not in STOPWORDS and len(w) > 1]


def keywords_for(job: Any, analysis: dict[str, Any] | None = None, names: dict[str, str] | None = None) -> list[str]:
    """The keywords an ATS would look for in this posting, most important first, at most MAX_KEYWORDS."""
    structured = getattr(job, "structured_description", None) or {}
    words: list[str] = []
    for key in ("required_skills", "preferred_skills", "keywords"):
        words += [str(w) for w in structured.get(key) or [] if str(w).strip()]
    if not words:
        words = [str(w) for w in getattr(job, "skills", None) or [] if str(w).strip()]
    if not words and analysis:  # older jobs: the ranking's own keyword list, else its matched requirements
        names = names or {}
        for key in ("keywords", "strong_matches"):
            words += [names.get(w, w) if isinstance(w, str) else str(w) for w in analysis.get(key) or []]
    out, seen = [], set()
    for word in words:
        key = normalize(word)
        if key and key not in seen and len(key) <= 60:
            seen.add(key)
            out.append(word.strip())
    return out[:MAX_KEYWORDS]


def coverage(job: Any, markdown: str, analysis: dict[str, Any] | None = None, names: dict[str, str] | None = None,
             gaps: str = "") -> list[dict[str, str]]:
    """[{"word", "status": covered | partial | missing}] for the resume text."""
    text = normalize((markdown or "").split("## Source Resume")[0])
    gap_text = normalize(gaps)
    out = []
    for word in keywords_for(job, analysis, names):
        needle = normalize(word)
        significant = _significant(needle)
        if _contains(text, needle):
            status = "covered"
        elif (len(significant) > 1 and sum(_contains(text, w) for w in significant) * 2 >= len(significant) + 1) \
                or (gap_text and _contains(gap_text, needle)):
            status = "partial"
        else:
            status = "missing"
        out.append({"word": word, "status": status})
    return out


def score(items: list[dict[str, str]]) -> int | None:
    """Covered plus half of partial, as a percentage of the keywords shown. None when there are no keywords."""
    if not items:
        return None
    points = sum(1 for i in items if i["status"] == "covered") + 0.5 * sum(1 for i in items if i["status"] == "partial")
    return round(100 * points / len(items))


def _level(job: Any, word: str) -> str:
    structured = getattr(job, "structured_description", None) or {}
    key = normalize(word)
    for field, level in (("required_skills", "REQUIRED"), ("preferred_skills", "PREFERRED")):
        if any(normalize(str(w)) == key for w in structured.get(field) or []):
            return level
    return "KEYWORD"


def targets(job: Any, vocabulary: set[str], sources: list[dict[str, Any]], analysis: dict[str, Any] | None = None,
            names: dict[str, str] | None = None) -> dict[str, list]:
    """The posting's keywords (the same list as the ATS score) split by what the candidate can truthfully claim.

        use         every significant word is in `vocabulary` (app.services.tailoring_results.claim_vocabulary, the
                    set validation accepts), with the posting's spelling, its level and `found_in`: the ids of the
                    `sources` ({"id", "text"}: source bullets, evidence) that mention it, for the bullet to cite
        do_not_use  the rest: requirements the candidate's facts do not cover, which stay gaps

    Deterministic, no model. Matching is per word, so "Node.js" in the resume makes "Node.js" usable."""
    from app.services.tailoring_results import NAME_PARTS_RE, _words

    known = vocabulary | {part for word in vocabulary for part in NAME_PARTS_RE.split(word) if part}
    texts = [(item["id"], normalize(item.get("text") or "")) for item in sources if item.get("id") is not None]
    use, do_not_use = [], []
    for word in keywords_for(job, analysis, names):
        parts = [p for w in _words(word) for p in NAME_PARTS_RE.split(w) if p and p not in STOPWORDS]
        if parts and all(p in known for p in parts):
            needle = normalize(word)
            found = list(dict.fromkeys(i for i, text in texts if _contains(text, needle)))[:5]
            use.append({"keyword": word, "level": _level(job, word), **({"found_in": found} if found else {})})
        else:
            do_not_use.append(word)
    order = {"REQUIRED": 0, "PREFERRED": 1, "KEYWORD": 2}
    return {"use": sorted(use, key=lambda item: order[item["level"]]), "do_not_use": do_not_use}
