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


# Evidence citations a model leaves in prose: "(evidence id 24)", "(evidence ids 36, 37)", "[E12]", ", evidence ID E3".
# The ids belong in the structured output, never in the text a recruiter reads.
_EVIDENCE_REF = r"#?E?\d+(?:\s*(?:,|and|&)\s*#?E?\d+)*"
CITATION_RES = (
    re.compile(r"\s*[(\[]\s*(?:verified\s+)?evidence\s+ids?\s*[:#]?\s*" + _EVIDENCE_REF + r"\s*[)\]]", re.IGNORECASE),
    re.compile(r"\s*[(\[]\s*E\d+(?:\s*,\s*E\d+)*\s*[)\]]"),
    re.compile(r",?\s*\b(?:verified\s+)?evidence\s+ids?\s*[:#]?\s*" + _EVIDENCE_REF + r"\b", re.IGNORECASE),
)


def strip_citations(text: str) -> str:
    """Remove evidence-id citations from prose meant for a reader, leaving the sentence around them intact."""
    for pattern in CITATION_RES:
        text = pattern.sub("", text or "")
    text = re.sub(r"[ \t]+([.,;:!?)])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text)


DATE_RANGE_RE = re.compile(
    r"((?:[A-Za-z]{3,9}\.?\s+)?\d{4}|\d{1,2}/\d{4})\s*(?:-|–|—|to)\s*"
    r"((?:[A-Za-z]{3,9}\.?\s+)?\d{4}|\d{1,2}/\d{4}|present|current|now|today)", re.IGNORECASE)


LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def unbold(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text or "").strip()


def flatten_links(text: str) -> str:
    """`[rohan@x.com](mailto:rohan@x.com)` -> `rohan@x.com`: exports show the address, not Markdown syntax."""
    def replace(match: re.Match[str]) -> str:
        label, target = match.group(1).strip(), match.group(2).strip()
        clean = re.sub(r"^(?:mailto:|tel:)", "", target)
        return label if label.lower() in (clean.lower(), target.lower()) else f"{label} ({clean})"

    return LINK_RE.sub(replace, text or "")


@dataclass
class Role:
    header: str
    bullets: list[str] = field(default_factory=list)
    # Lines between the role heading and its first bullet, e.g. "**Flow AI** | Remote, USA" and "Oct 2025 - Present".
    # Resumes that put the employer and dates under the title keep them here instead of losing them to the bullets.
    meta: list[str] = field(default_factory=list)

    @property
    def parts(self) -> list[str]:
        return [unbold(p) for p in self.header.split("|") if unbold(p)] or [""]

    @property
    def meta_parts(self) -> list[str]:
        return [unbold(p) for line in self.meta for p in line.split("|") if unbold(p)]

    @property
    def title(self) -> str:
        return self.parts[0]

    @property
    def company(self) -> str:
        if len(self.parts) > 1:
            return self.parts[1]
        rest = [p for p in self.meta_parts if not DATE_RANGE_RE.search(p)]
        return rest[0] if rest else ""

    @property
    def location(self) -> str:
        if len(self.parts) > 3:
            return self.parts[2]
        rest = [p for p in self.meta_parts if not DATE_RANGE_RE.search(p)]
        if len(self.parts) > 1:  # "Title | Company" with "Location | Dates" written under it
            return rest[0] if rest else ""
        return rest[1] if len(rest) > 1 else ""

    @property
    def dates(self) -> str:
        if len(self.parts) > 2:
            return self.parts[-1]
        return next((p for p in self.meta_parts if DATE_RANGE_RE.search(p)), "")

    @property
    def full_header(self) -> str:
        """Title, employer, location and dates on one line, wherever the master resume put them."""
        return " | ".join(p for p in (self.title, self.company, self.location, self.dates) if p)

    def heading_lines(self, title: str | None = None, company: str | None = None) -> list[str]:
        """The role heading as rendered: one "### " line, plus the location on its own line when there are no
        dates (on the heading, "Title | Company | Location" would read the location as the dates)."""
        title, company = title or self.title, company if company is not None else self.company
        if self.location and not self.dates and company:
            return [f"### {' | '.join(p for p in (title, company) if p)}", self.location]
        return [f"### {' | '.join(p for p in (title, company, self.location, self.dates) if p)}"]


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
            last_kind = "role"
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
        if current.kind == "experience" and current.roles and not current.roles[-1].bullets:
            # Employer, location and dates written under the role heading: role metadata, not an achievement.
            current.roles[-1].meta.append(line)
            last_kind = "meta"
        elif current.kind == "experience" and current.roles and last_kind != "para":
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


# "**Data and Streaming:** Kafka, Spark" or "Data and Streaming: Kafka, Spark" -> label, then the comma list.
SKILL_GROUP_RE = re.compile(r"^(?P<label>(?:\*\*)?[^:]{1,80}:(?:\*\*)?\s*)(?P<values>.+,.+)$")


def _mentioned(text: str, skills: list[str]) -> int:
    """How many of the tailored skills this line names."""
    haystack = f" {to_plain(text).lower()} "
    return sum(bool(re.search(rf"(?<![\w+#]){re.escape(skill.lower())}(?![\w+#])", haystack)) for skill in skills)


def order_skills(lines: list[str], top_skills: list[str]) -> list[str]:
    """The skills section as bullets, most relevant first.

    Nothing is added, removed or reworded: the groups this job asks for lead, and inside each group the skills
    it asks for come first. A tailored resume therefore never repeats a "most relevant" list of its own.
    """
    ordered = sorted(lines, key=lambda line: -_mentioned(line, top_skills)) if top_skills else list(lines)
    out = []
    for line in ordered:
        body = line.removeprefix("- ")
        group = SKILL_GROUP_RE.match(body)
        if group and top_skills:
            items = [item.strip() for item in group.group("values").split(",") if item.strip()]
            wanted = [item for skill in top_skills for item in items if item.lower() == skill.lower()]
            body = f"{group.group('label')}{', '.join(dict.fromkeys(wanted + items))}"
        out.append(f"- {body}")
    return out


def render_resume(parsed: ParsedResume, *, name: str = "", headline: str = "", summary: str | None = None,
                  top_skills: list[str] | None = None, role_bullets: dict[int, list[str]] | None = None,
                  highlights: list[str] | None = None, role_headings: dict[int, dict[str, str]] | None = None,
                  role_caps: dict[int, int] | None = None, fallback_bullets: dict[int, list[str]] | None = None) -> str:
    """`role_headings` replaces a role's title and employer (the user's verified corrections); its location and
    dates stay as the master resume wrote them.

    `role_caps` (a tailored resume's length plan, app.services.resume_length) caps each role's bullets: a role the
    tailoring left alone shows its first `cap` bullets (`fallback_bullets`, most relevant first, else as written),
    and a cap of 0 keeps the heading only. Without caps every role shows all its bullets, as before."""
    role_bullets, role_headings = role_bullets or {}, role_headings or {}
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
        if section.kind == "skills":
            out.extend(order_skills(section.lines, top_skills or []))
            continue
        if section.kind == "experience":
            out.extend(section.lines)
            for index, role in enumerate(section.roles):
                bullets = [b for b in role_bullets.get(index, []) if b and b.strip()]
                if role_caps is None:
                    bullets = bullets or role.bullets
                else:
                    cap = role_caps.get(index, len(role.bullets))
                    bullets = (bullets or (fallback_bullets or {}).get(index) or role.bullets)[:cap]
                fix = role_headings.get(index)
                heading = role.heading_lines(fix["title"], fix["company"]) if fix else role.heading_lines()
                out += ["", *heading, *[f"- {to_plain(b).strip()}" for b in bullets]]
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
