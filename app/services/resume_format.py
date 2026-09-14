"""Agent-friendly resume format: normalize, parse and rebuild.

The master resume is plain text in a small Markdown dialect that people, LLMs and ATS parsers
all read the same way:

    # Full Name
    Headline
    City | email | phone | linkedin | github
    ## Summary
    One paragraph.
    ## Skills
    - Category: a, b, c
    ## Experience
    ### Title | Company | Location | Mon YYYY - Mon YYYY
    - One achievement per bullet.
    ## Projects / ## Education / any other section

Tailoring rewrites the summary, highlights relevant skills and rewrites bullets per role, while
every role header, project and education line comes from the master resume unchanged.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Characters the built-in PDF fonts cannot draw, mapped to plain equivalents.
REPLACEMENTS = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "•": "-", "‣": "-", "⁃": "-", "▪": "-", "●": "-", "◦": "-", "·": "-",
    "×": "x", "…": "...", "→": "->", "←": "<-", "≈": "~", "≤": "<=", "≥": ">=",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "", "­": "",
}
BULLET_RE = re.compile(r"^\s*(?:[-*•●▪‣⁃◦]|\d+[.)])\s+")
SECTION_KINDS = [
    ("summary", ("summary", "profile", "about", "objective")),
    ("skills", ("skill", "competenc", "technolog", "tool")),
    ("experience", ("experience", "employment", "work history", "career")),
    ("projects", ("project",)),
    ("education", ("education", "academic", "degree")),
]


def to_plain(text: str) -> str:
    """Normalize to characters every export font can render (Windows-1252 at most)."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(REPLACEMENTS.get(ch, ch) for ch in text)
    out = []
    for ch in text:
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode())
    return "".join(out)


@dataclass
class Role:
    header: str
    bullets: list[str] = field(default_factory=list)

    @property
    def parts(self) -> list[str]:
        return [p.strip() for p in self.header.split("|")]

    @property
    def title(self) -> str:
        return self.parts[0]

    @property
    def company(self) -> str:
        return self.parts[1] if len(self.parts) > 1 else ""

    @property
    def dates(self) -> str:
        return self.parts[-1] if len(self.parts) > 2 else ""


@dataclass
class Section:
    title: str
    kind: str
    lines: list[str] = field(default_factory=list)   # markdown lines: paragraphs, "- " bullets, "### " sub-headers
    roles: list[Role] = field(default_factory=list)


@dataclass
class ParsedResume:
    name: str = ""
    header_lines: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def section(self, kind: str) -> Section | None:
        return next((s for s in self.sections if s.kind == kind), None)

    @property
    def roles(self) -> list[Role]:
        exp = self.section("experience")
        return exp.roles if exp else []

    @property
    def structured(self) -> bool:
        return bool(self.roles)


def section_kind(title: str) -> str:
    lowered = title.lower()
    return next((kind for kind, words in SECTION_KINDS if any(w in lowered for w in words)), "other")


def parse_resume(text: str) -> ParsedResume:
    parsed = ParsedResume()
    current: Section | None = None
    last_kind = ""  # "bullet" or "para": where a wrapped continuation line should be appended

    def append_continuation(line: str) -> bool:
        if not current or last_kind not in ("bullet", "para"):
            return False
        target = current.roles[-1].bullets if current.kind == "experience" and current.roles and last_kind == "bullet" else current.lines
        if not target:
            return False
        target[-1] = f"{target[-1].rstrip()} {line}"
        return True

    for raw in to_plain(text).splitlines():
        line = raw.strip()
        if not line:
            last_kind = ""
            continue
        if line.startswith("## ") or (line.startswith("#") and not line.startswith("# ") and not line.startswith("###")):
            title = line.lstrip("#").strip()
            current = Section(title=title, kind=section_kind(title))
            parsed.sections.append(current)
            last_kind = ""
            continue
        if line.startswith("# ") and not parsed.name:
            parsed.name = line[2:].strip()
            continue
        if current is None:
            parsed.header_lines.append(line)
            continue
        if line.startswith("### "):
            if current.kind == "experience":
                current.roles.append(Role(header=line[4:].strip()))
            else:
                current.lines.append(line)
            last_kind = ""
            continue
        if BULLET_RE.match(line):
            item = BULLET_RE.sub("", line).strip()
            if current.kind == "experience" and current.roles:
                current.roles[-1].bullets.append(item)
            else:
                current.lines.append(f"- {item}")
            last_kind = "bullet"
            continue
        # A lowercase start or a previous line without end punctuation means a hard-wrapped line.
        wrapped = line[:1].islower() or (last_kind == "bullet" and not re.search(r"[.;:!?)]$", _last_text(current)))
        if wrapped and append_continuation(line):
            continue
        if current.kind == "experience" and current.roles and last_kind != "para":
            current.roles[-1].bullets.append(line)
            last_kind = "bullet"
        else:
            current.lines.append(line)
            last_kind = "para"
    return parsed


def _last_text(section: Section | None) -> str:
    if not section:
        return ""
    if section.kind == "experience" and section.roles and section.roles[-1].bullets:
        return section.roles[-1].bullets[-1]
    return section.lines[-1] if section.lines else ""


def summary_text(parsed: ParsedResume) -> str:
    section = parsed.section("summary")
    return " ".join(line.lstrip("- ").strip() for line in section.lines) if section else ""


def skills_text(parsed: ParsedResume) -> str:
    section = parsed.section("skills")
    return "\n".join(section.lines) if section else ""


def relevant_skills(parsed: ParsedResume, tailored: list[str]) -> list[str]:
    """Keep only tailored skills that already appear in the master resume, preserving their order."""
    haystack = " ".join(line for s in parsed.sections for line in s.lines).lower()
    haystack += " " + " ".join(b for r in parsed.roles for b in r.bullets).lower()
    seen, kept = set(), []
    for skill in tailored:
        clean = to_plain(str(skill)).strip()
        if clean and clean.lower() in haystack and clean.lower() not in seen:
            kept.append(clean)
            seen.add(clean.lower())
    return kept


def render_resume(parsed: ParsedResume, *, name: str = "", headline: str = "", summary: str | None = None,
                  top_skills: list[str] | None = None, role_bullets: dict[int, list[str]] | None = None,
                  highlights: list[str] | None = None) -> str:
    role_bullets = role_bullets or {}
    out = [f"# {parsed.name or name or 'Candidate'}"]
    out.extend(parsed.header_lines or ([headline] if headline else []))
    sections = list(parsed.sections)
    if summary and not parsed.section("summary"):
        sections.insert(0, Section("Summary", "summary"))
    for section in sections:
        out += ["", f"## {section.title}"]
        if section.kind == "summary" and summary:
            out.append(to_plain(summary).strip())
            if highlights:
                out += ["", "## Highlights", *[f"- {to_plain(h).strip()}" for h in highlights if h.strip()]]
            continue
        if section.kind == "skills" and top_skills:
            out.append(f"- Most relevant: {', '.join(top_skills)}")
        if section.kind == "experience":
            out.extend(section.lines)
            for index, role in enumerate(section.roles):
                bullets = [b for b in role_bullets.get(index, []) if b and b.strip()] or role.bullets
                out += ["", f"### {role.header}", *[f"- {to_plain(b).strip()}" for b in bullets]]
            continue
        out.extend(section.lines)
    return "\n".join(out).strip() + "\n"


def render_legacy(name: str, headline: str, summary: str, skills: list[str], bullets: list[str]) -> str:
    """Fallback when the master resume has no ### role lines: a compact resume, never the raw source."""
    out = [f"# {name or 'Candidate'}"]
    if headline:
        out.append(to_plain(headline))
    out += ["", "## Summary", to_plain(summary).strip()]
    if skills:
        out += ["", "## Skills", f"- {', '.join(to_plain(s) for s in skills)}"]
    if bullets:
        out += ["", "## Selected Experience", *[f"- {to_plain(b).strip()}" for b in bullets if b.strip()]]
    return "\n".join(out).strip() + "\n"


def body_text(markdown: str) -> str:
    """Resume text without any legacy appended source section."""
    return (markdown or "").split("## Source Resume")[0]
