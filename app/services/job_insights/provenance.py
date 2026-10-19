"""Provenance of a research package's sections, checked in code before they are saved (Phase 4, VALIDATE_PROVENANCE).

`check` lists every item whose provenance breaks a rule; `enforce` drops those items and returns the sections with
what is left. Pure functions over `service.build_sections` output and the insight's current observations: no model,
no database. The synthesis validators (`synthesis.validate_company` / `validate_interview`) already reject a model
answer that cites an id it was not shown; this is the second, independent check, over the sections as they will be
stored, so a regression anywhere between the model and the table cannot label an estimate as the employer's band or a
generated question as a reported one.

Rules (each item is kept apart by kind, never re-labelled):
  * official values cite only OFFICIAL_COMPANY observations of category VALUES;
  * culture themes (employee-reported) cite only employee sources (CULTURE / SENTIMENT), never the company's pages;
  * a FACT or DERIVED company item cites at least one observation, and only observations of this insight;
  * reported interview stages and reported questions cite observations of interview categories;
  * generated questions, questions to ask and value preparation are GENERATED and carry no source attribution;
  * compensation: the employer range comes only from EMPLOYER / JOB_DESCRIPTION observations, the recruiter budget
    only from RECRUITER, the market range only from market sources. Compensation is computed in code, so a breach
    there is never "fixed" by editing a figure: `enforce` withholds the run's compensation and its preparation whole,
    and the last stored version stands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.compensation import MARKET_SOURCES

EMPLOYER_PAY = ("EMPLOYER", "JOB_DESCRIPTION")
EMPLOYEE_CATEGORIES = ("CULTURE", "SENTIMENT")
INTERVIEW_CATEGORIES = ("INTERVIEW_PROCESS", "INTERVIEW_QUESTION")
GENERATED_ORIGINS = ("ROLE_DERIVED", "CANDIDATE_SPECIFIC")
ATTRIBUTION_KEYS = ("source_ids", "sources", "source_type", "reported_role", "reported_at")


@dataclass
class Report:
    violations: list[dict[str, Any]] = field(default_factory=list)

    def add(self, section: str, index: int | None, rule: str) -> None:
        self.violations.append({"section": section, "index": index, "rule": rule})

    @property
    def count(self) -> int:
        return len(self.violations)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v["rule"]] = out.get(v["rule"], 0) + 1
        return out


def _ids(item: dict[str, Any], key: str = "source_ids") -> list[Any]:
    value = item.get(key)
    return value if isinstance(value, list) else []


def _cites(item: dict[str, Any], by_id: dict[int, dict[str, Any]], *, categories: tuple[str, ...] = (),
           source_types: tuple[str, ...] = (), not_source_types: tuple[str, ...] = (), key: str = "source_ids",
           required: bool = True) -> str | None:
    """None when every cited observation exists and fits; else the rule it breaks."""
    ids = _ids(item, key)
    if not ids:
        return "MISSING_SOURCE" if required else None
    for i in ids:
        o = by_id.get(i) if isinstance(i, int) else None
        if o is None:
            return "UNKNOWN_SOURCE"
        if categories and o.get("category") not in categories:
            return "WRONG_CATEGORY"
        if source_types and o.get("source_type") not in source_types:
            return "WRONG_SOURCE_TYPE"
        if not_source_types and o.get("source_type") in not_source_types:
            return "WRONG_SOURCE_TYPE"
    return None


def _walk(sections: dict[str, Any], by_id: dict[int, dict[str, Any]], report: Report) -> dict[tuple[str, int], str]:
    """(section path, index) → rule, for every list item that breaks a rule."""
    bad: dict[tuple[str, int], str] = {}

    def flag(path: str, index: int, rule: str | None) -> None:
        if rule:
            bad[(path, index)] = rule
            report.add(path, index, rule)

    company = sections.get("company") or {}
    for i, item in enumerate(company.get("official_values") or []):
        flag("company.official_values", i, _cites(item, by_id, categories=("VALUES",),
                                                  source_types=("OFFICIAL_COMPANY",)))
    for i, item in enumerate(company.get("culture_themes") or []):
        flag("company.culture_themes", i, _cites(item, by_id, categories=EMPLOYEE_CATEGORIES,
                                                 not_source_types=("OFFICIAL_COMPANY",)))
    for i, item in enumerate(company.get("profile_facts") or []):
        flag("company.profile_facts", i, _cites(item, by_id))
    for i, item in enumerate(company.get("mission") or []):
        flag("company.mission", i, _cites(item, by_id, source_types=("OFFICIAL_COMPANY",)))
    summary = company.get("summary")
    if isinstance(summary, dict):
        rule = _cites(summary, by_id)
        if rule:
            bad[("company.summary", -1)] = rule
            report.add("company.summary", None, rule)

    process = sections.get("interview_process") or {}
    for i, stage in enumerate(process.get("stages") or []):
        if stage.get("origin") == "REPORTED":
            flag("interview_process.stages", i, _cites(stage, by_id, categories=INTERVIEW_CATEGORIES))
        elif stage.get("kind") == "FACT":
            flag("interview_process.stages", i, "INFERRED_LABELLED_FACT")

    for i, item in enumerate(sections.get("reported_questions") or []):
        rule = "NOT_REPORTED" if item.get("origin") != "REPORTED" else \
            _cites(item, by_id, categories=("INTERVIEW_QUESTION",))
        flag("reported_questions", i, rule)

    for i, item in enumerate(sections.get("generated_questions") or []):
        rule = None
        if item.get("kind") != "GENERATED" or item.get("origin") not in GENERATED_ORIGINS:
            rule = "GENERATED_MISLABELLED"
        elif any(item.get(k) for k in ATTRIBUTION_KEYS):
            rule = "GENERATED_WITH_SOURCE"
        flag("generated_questions", i, rule)
    for name in ("questions_to_ask", "value_prep"):
        for i, item in enumerate(sections.get(name) or []):
            flag(name, i, "GENERATED_MISLABELLED" if item.get("kind") != "GENERATED" else None)

    for i, item in enumerate(sections.get("risks") or []):
        flag("risks", i, _cites(item, by_id, required=False))
    # Application-form drafts: a draft is GENERATED and never cites research; a library answer is the person's own.
    for i, item in enumerate(sections.get("application_questions") or []):
        status, kind = item.get("status"), item.get("kind")
        rule = None
        if status == "DRAFTED" and (kind != "GENERATED" or any(item.get(k) for k in ATTRIBUTION_KEYS)):
            rule = "GENERATED_MISLABELLED" if kind != "GENERATED" else "GENERATED_WITH_SOURCE"
        elif status == "LIBRARY" and not item.get("answer_source"):
            rule = "MISSING_SOURCE"
        elif status not in ("DRAFTED", "LIBRARY") and item.get("answer"):
            rule = "GENERATED_MISLABELLED"
        flag("application_questions", i, rule)
    return bad


def _compensation(sections: dict[str, Any], by_id: dict[int, dict[str, Any]], report: Report) -> bool:
    """Report each compensation figure that cites the wrong kind of source. True when there was one."""
    breached = False
    comp = sections.get("compensation") or {}
    checks = (("employer_posted_range", EMPLOYER_PAY), ("recruiter_budget", ("RECRUITER",)),
              ("market_range", MARKET_SOURCES))
    for name, allowed in checks:
        value = comp.get(name)
        if isinstance(value, dict):
            rule = _cites(value, by_id, source_types=allowed, categories=("COMPENSATION",))
            if rule:
                report.add(f"compensation.{name}", None, rule)
                breached = True
    return breached


def check(sections: dict[str, Any], observations: list[dict[str, Any]]) -> Report:
    """Every provenance problem in `sections`, against the insight's current observations."""
    by_id = {o["id"]: o for o in observations}
    report = Report()
    _walk(sections, by_id, report)
    _compensation(sections, by_id, report)
    return report


def enforce(sections: dict[str, Any], observations: list[dict[str, Any]]) -> tuple[dict[str, Any], Report]:
    """`sections` without the items whose provenance breaks a rule (dropped, never re-labelled), and the report."""
    by_id = {o["id"]: o for o in observations}
    report = Report()
    bad = _walk(sections, by_id, report)
    comp_breached = _compensation(sections, by_id, report)
    if not bad and not comp_breached:
        return sections, report
    out = dict(sections)
    if comp_breached:
        # Compensation is computed in code as one picture (status, ranges, discussion range, preparation), so a figure
        # is never cut out of it or edited: the run's compensation is withheld whole, and the last stored one stands.
        out.pop("compensation", None)
        out.pop("compensation_prep", None)

    def keep(path: str, items: list[Any]) -> list[Any]:
        return [item for i, item in enumerate(items) if (path, i) not in bad]

    if isinstance(out.get("company"), dict):
        company = dict(out["company"])
        for key in ("official_values", "culture_themes", "profile_facts", "mission"):
            company[key] = keep(f"company.{key}", company.get(key) or [])
        if ("company.summary", -1) in bad:
            company["summary"] = None
        out["company"] = company
    if isinstance(out.get("interview_process"), dict):
        process = dict(out["interview_process"])
        process["stages"] = keep("interview_process.stages", process.get("stages") or [])
        out["interview_process"] = process
    for name in ("reported_questions", "generated_questions", "questions_to_ask", "value_prep", "risks",
                 "application_questions"):
        if isinstance(out.get(name), list):
            out[name] = keep(name, out[name])
    return out, report
