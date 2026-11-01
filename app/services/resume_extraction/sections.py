"""[F] Where each section of a resume starts and what it is. Deterministic: a synonym table, keywords and the page's
own layout; never a model.

A heading is a short line on its own, without a date, a bar or an e-mail address, that either

    matches a known heading          "Core Skills", "Areas of Expertise", "Career History"     method synonym
    contains a section keyword       "Technical Expertise & Tools", "Relevant Work Experience"   method keyword
    looks like a heading             larger than the body, in capitals, or a Word heading style  method unknown

A heading the table cannot place is still a heading: it starts its own section (kind "other") so its lines never run
on into the section before it, and the person can assign it on review. Without layout (a text file) only the first
two methods count, since a short line alone is no evidence of a heading.

The lines above the first placed heading are the header (name, headline, contact). A layout-only heading there is
part of the header too: a headline in capitals ("AI ENGINEER") is not a section.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.services.resume_extraction.models import LineInfo

KINDS = ("summary", "skills", "experience", "projects", "education", "certifications", "awards", "languages",
         "additional_information")
OTHER = "other"
# What each kind is rendered as for the parser (structured_resume understands these headings).
LABELS = {"summary": "Summary", "skills": "Skills", "experience": "Experience", "projects": "Projects",
          "education": "Education", "certifications": "Certifications", "awards": "Awards", "languages": "Languages",
          "additional_information": "Additional information", OTHER: "Additional information"}

SYNONYMS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "professional summary", "profile", "professional profile", "about", "about me",
                "objective", "career objective", "overview", "career summary", "executive summary", "introduction",
                "personal statement", "summary of qualifications", "professional overview"),
    "skills": ("skills", "technical skills", "core skills", "key skills", "skill set", "skillset", "core competencies",
               "competencies", "areas of expertise", "expertise", "technical expertise", "tech stack",
               "technology stack", "technologies", "tools", "tools and technologies", "toolkit",
               "technical proficiencies", "proficiencies", "strengths", "skills and tools", "skills and expertise"),
    "experience": ("experience", "work experience", "professional experience", "employment", "employment history",
                   "work history", "career history", "professional background", "relevant experience", "career",
                   "positions held", "experience history", "industry experience", "professional history"),
    "projects": ("projects", "personal projects", "selected projects", "key projects", "side projects",
                 "notable projects", "project experience", "portfolio", "open source"),
    "education": ("education", "academic background", "academics", "education and training", "qualifications",
                  "academic qualifications", "educational background", "education and certifications"),
    "certifications": ("certifications", "certificates", "certification", "licenses", "licenses and certifications",
                       "accreditations", "courses", "training", "professional development"),
    "awards": ("awards", "honors", "honours", "awards and honors", "awards and honours", "achievements",
               "recognition", "accomplishments"),
    "languages": ("languages", "language skills", "spoken languages"),
    "additional_information": ("additional information", "interests", "hobbies", "volunteering",
                               "volunteer experience", "publications", "references", "activities",
                               "extracurricular activities", "other", "miscellaneous"),
}
_EXACT = {phrase: kind for kind, phrases in SYNONYMS.items() for phrase in phrases}
KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("additional_information", ("volunteer", "publication", "interest", "hobb")),
    ("certifications", ("certif", "licen")),
    ("skills", ("skill", "competenc", "expertise", "stack", "technolog", "tools", "proficienc")),
    ("experience", ("experience", "employment", "work history", "career")),
    ("projects", ("project",)),
    ("education", ("educat", "academ", "degree", "qualification")),
    ("awards", ("award", "honor", "honour")),
    ("languages", ("language",)),
    ("summary", ("summary", "profile", "objective")),
)
MAX_WORDS, MAX_CHARS = 6, 48
HEADING_SIZE = 1.1  # a heading's glyphs at least this much taller than the body's

_DATE = re.compile(r"\b(?:19|20)\d{2}\b|\bpresent\b", re.IGNORECASE)
_NOT_HEADING = re.compile(r"[|@•]|https?://|www\.|\d{3}")
_BULLET = re.compile(r"^\s*(?:[-*•●▪‣⁃◦]|\d+[.)])\s+")


@dataclass(frozen=True)
class Heading:
    line: int  # index into the document's lines
    text: str  # as written
    kind: str  # one of KINDS, or "other"
    method: str  # synonym | keyword | unknown
    signal: str  # what made it a heading: text | layout | style

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def phrase(text: str) -> str:
    """A heading as the synonym table writes it: lower case, "&" as "and", no punctuation around it."""
    out = (text or "").strip().strip(":").replace("&", " and ").lower()
    return " ".join(re.sub(r"[^a-z0-9 /]", " ", out).split())


def classify(text: str) -> tuple[str, str]:
    """(kind, method) for a heading's text."""
    p = phrase(text)
    if p in _EXACT:
        return _EXACT[p], "synonym"
    for kind, words in KEYWORDS:
        if any(w in p for w in words):
            return kind, "keyword"
    return OTHER, "unknown"


def _shape(line: LineInfo) -> bool:
    """Short, alone, no date, bar, address or sentence: the shape a heading has."""
    text = line.text.strip()
    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS or _BULLET.match(text):
        return False
    if _DATE.search(text) or _NOT_HEADING.search(text) or text.rstrip(":").endswith((".", ",", ";")):
        return False
    return any(c.isalpha() for c in text)


def _body_size(lines: list[LineInfo]) -> float | None:
    sizes = sorted(line.size for line in lines if line.size and len(line.text) > MAX_CHARS)
    return sizes[len(sizes) // 2] if sizes else None


def _looks_like_heading(line: LineInfo, body: float | None) -> bool:
    letters = [c for c in line.text if c.isalpha()]
    caps = len(letters) >= 3 and all(c.isupper() for c in letters)
    larger = bool(body and line.size and line.size >= HEADING_SIZE * body)
    return caps or larger or bool(line.style and line.style.lower().startswith(("heading", "title")))


_SMALL = {"and", "of", "the", "in", "for", "to", "&", "a", "an"}


def _title_like(text: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][\w'-]*|&", text) if w.lower() not in _SMALL]
    return bool(words) and all(w[0].isupper() for w in words)


def _caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _house_style(lines: list[LineInfo], candidates: list[tuple[int, str, str]]) -> tuple[bool | None, float | None]:
    """How this document writes its section headings, learnt from the ones the synonym table knows: in capitals or
    not, and how tall. (None, None) when no such heading has a look to learn from."""
    known = [lines[i] for i, _, method in candidates if method == "synonym" and (lines[i].size or lines[i].style)]
    if not known:
        return None, None
    caps = sum(_caps(line.text) for line in known) * 2 > len(known)
    sizes = sorted(line.size for line in known if line.size)
    return caps, (sizes[len(sizes) // 2] if sizes else None)


def _in_style(line: LineInfo, caps: bool | None, size: float | None) -> bool:
    if caps is None:
        return True
    if _caps(line.text) != caps:
        return False
    return not (size and line.size) or abs(line.size - size) <= 0.08 * size


def detect(lines: list[LineInfo]) -> list[Heading]:
    """Every section heading, in order.

    A synonym is a heading wherever it stands. A keyword or an unknown heading also needs to look like this
    document's other headings (its house style: capitals, size), so a role title or a project name in bold is never
    taken for one, and nothing above the first synonym heading is one (the header: "Full Stack Developer")."""
    layout = any(line.size or line.style for line in lines)
    body = _body_size(lines)
    candidates = [(i, *classify(line.text)) for i, line in enumerate(lines) if _shape(line)]
    caps, size = _house_style(lines, candidates) if layout else (None, None)
    out: list[Heading] = []
    for i, kind, method in candidates:
        line = lines[i]
        styled = _looks_like_heading(line, body) if layout else False
        if method != "synonym":
            if not out:
                continue  # the header: a headline is not a section, whatever it says
            if layout and not (styled and _in_style(line, caps, size)):
                continue  # not written like this document's headings
            if not layout and (method == "unknown" or "," in line.text or not _title_like(line.text)):
                continue  # without layout only a heading written like one counts
        signal = "style" if line.style else "layout" if styled else "text"
        out.append(Heading(line=i, text=line.text.strip(), kind=kind, method=method, signal=signal))
    return out


def section_of(headings: list[Heading], index: int) -> str | None:
    """The kind of section a line belongs to (None: the header)."""
    kind = None
    for h in headings:
        if h.line > index:
            break
        kind = h.kind
    return kind
