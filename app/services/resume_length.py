"""How long a tailored resume may be: a page target and a top-down bullet budget per role. Deterministic, no model.

    page target (Settings default, or chosen for one job) → budgets, newest role first → role_caps per role_index

The structured resume is the foundation a tailored resume is built from, not a checklist: the Tailor chooses which
bullets fill each role's budget, and code enforces the budget (tailoring_results, workflow.build_resume_markdown).
The most recent role gets the most bullets; older roles fewer; a budget of 0 keeps the role's heading (title,
employer, dates) with no bullets, so the work history never has a gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PAGES = (1, 2, 3)
DEFAULT_PAGES = 2
MAX_POSITIONS, MAX_BULLETS = 12, 8  # RoleUpdate.bullets allows at most 8
# Bullets per role, most recent role first; the last value repeats for every older role.
PRESETS: dict[int, tuple[int, ...]] = {
    1: (4, 3, 2, 1, 0),
    2: (6, 5, 4, 3, 2, 1),
    3: (7, 6, 5, 4, 3, 2),
}
PAGE_LABELS = {1: "1 page", 2: "2 pages", 3: "3 pages"}
# A summary sentence budget the prompt states (the renderer does not cut sentences).
SUMMARY_SENTENCES = {1: 3, 2: 4, 3: 4}
# Roughly what fits on a resume page (a word count, plus headings and bullets counted as short lines).
WORDS_PER_PAGE, LINES_PER_PAGE = 520, 46


@dataclass(frozen=True)
class LengthPlan:
    pages: int
    budgets: tuple[int, ...]

    def cap(self, rank: int) -> int:
        """The bullet budget of the role at this recency rank (0 = most recent)."""
        if not self.budgets:
            return 0
        return self.budgets[rank] if rank < len(self.budgets) else self.budgets[-1]

    def as_dict(self) -> dict[str, Any]:
        return {"pages": self.pages, "budgets": list(self.budgets)}


def clean_budgets(values: Any, pages: int) -> tuple[int, ...]:
    """Edited budgets, each clamped to 0-8, at most 12 positions. Anything unusable falls back to the preset."""
    if not isinstance(values, (list, tuple)) or not values:
        return PRESETS[pages]
    out = []
    for value in list(values)[:MAX_POSITIONS]:
        try:
            out.append(max(0, min(MAX_BULLETS, int(value))))
        except (TypeError, ValueError):
            return PRESETS[pages]
    return tuple(out)


def clean_pages(value: Any, default: int = DEFAULT_PAGES) -> int:
    try:
        pages = int(value)
    except (TypeError, ValueError):
        return default
    return pages if pages in PAGES else default


def settings_value(saved: dict[str, Any] | None) -> dict[str, Any]:
    """The account's tailoring length as stored in preferences["tailoring"], cleaned: the default page target and
    the budget for every page target (edited, else the preset)."""
    saved = saved or {}
    budgets = saved.get("budgets") or {}
    return {"pages": clean_pages(saved.get("pages")),
            "budgets": {str(p): list(clean_budgets(budgets.get(str(p)), p)) for p in PAGES},
            "presets": {str(p): list(PRESETS[p]) for p in PAGES}}


def plan_for(db, user_id: int | None = None, pages: int | None = None) -> LengthPlan:
    """The plan for one tailoring run: the given page target (a per-job choice), else the account's default."""
    from app.services.user_settings import settings_for

    saved = settings_value((settings_for(db, user_id).preferences or {}).get("tailoring"))
    chosen = clean_pages(pages, saved["pages"]) if pages is not None else saved["pages"]
    return LengthPlan(chosen, tuple(saved["budgets"][str(chosen)]))


def plan_from(value: dict[str, Any] | None) -> LengthPlan | None:
    """A plan stored with a tailored resume (content["length_plan"]); None for packets made before plans."""
    if not isinstance(value, dict) or "pages" not in value:
        return None
    pages = clean_pages(value.get("pages"))
    return LengthPlan(pages, clean_budgets(value.get("budgets"), pages))


# ───────────── recency ─────────────

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), start=1)}
CURRENT = re.compile(r"\b(present|current|now|today|till date|to date)\b", re.IGNORECASE)
POINT = re.compile(r"(?P<iyear>(?:19|20)\d{2})-(?P<imonth>\d{2})\b|(?:(?P<month>[A-Za-z]{3,9})\.?\s+)?(?P<year>(?:19|20)\d{2})"
                   r"|(?P<num>\d{1,2})/(?P<nyear>(?:19|20)\d{2})")


def _points(dates: str) -> list[tuple[int, int]]:
    out = []
    for match in POINT.finditer(dates or ""):
        if match.group("iyear"):  # 2024-06, the structured data's normalized form
            out.append((int(match.group("iyear")), int(match.group("imonth"))))
        elif match.group("nyear"):
            out.append((int(match.group("nyear")), int(match.group("num"))))
        else:
            out.append((int(match.group("year")), MONTHS.get((match.group("month") or "")[:3].lower(), 0)))
    return out


def recency_key(dates: str, index: int) -> tuple:
    """Sort key, most recent first: a current role, then the latest end, then the latest start, then document
    order (resumes list the newest role first)."""
    points = _points(dates)
    current = bool(CURRENT.search(dates or ""))
    end = (9999, 12) if current else (points[-1] if points else (0, 0))
    start = points[0] if points else (0, 0)
    return (not current, (-end[0], -end[1]), (-start[0], -start[1]), index)


def role_caps(plan: LengthPlan, roles: list[Any]) -> dict[int, int]:
    """{role_index: max bullets} for the roles of the rendered resume (resume_format Role objects or dicts with
    "dates"), ranked by recency."""
    def dates_of(role: Any) -> str:
        return role.get("dates", "") if isinstance(role, dict) else getattr(role, "dates", "")

    order = sorted(range(len(roles)), key=lambda i: recency_key(dates_of(roles[i]), i))
    return {index: plan.cap(rank) for rank, index in enumerate(order)}


# ───────────── length estimate ─────────────

def estimate_pages(markdown: str) -> float:
    """A rough page count for a rendered resume: whichever of words or lines fills pages faster. Shown as a guide
    ("≈1.2 pages"), never used to cut anything."""
    lines = [line for line in (markdown or "").splitlines() if line.strip()]
    words = sum(len(line.split()) for line in lines)
    return round(max(words / WORDS_PER_PAGE, len(lines) / LINES_PER_PAGE), 1)
