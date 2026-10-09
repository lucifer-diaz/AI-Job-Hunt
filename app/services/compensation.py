"""Salary enrichment from the original job posting.

Deterministic and conservative: structured salary fields that job-board actors return are used
first, then explicit salary text fields. The free-text description is never mined, because numbers
in prose (team sizes, years of experience, revenue) are too easy to misread as pay.

Glassdoor and PayScale are researched separately, for approved jobs only, by Job Insights
(app.services.job_insights); `enrich_compensation` still records them as `not_configured` because the posting
enrichment never calls them. `compensation_insight` below compares the posting with those observations.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

MIN_KEYS = ("min_amount", "salary_min", "min_salary", "minSalary", "salaryMin", "salary_from", "ai_salary_minvalue")
MAX_KEYS = ("max_amount", "salary_max", "max_salary", "maxSalary", "salaryMax", "salary_to", "ai_salary_maxvalue")
CURRENCY_KEYS = ("currency", "salary_currency", "salaryCurrency", "ai_salary_currency")
PERIOD_KEYS = ("interval", "salary_interval", "salary_period", "salaryPeriod", "pay_period", "ai_salary_unittext")
NESTED_KEYS = ("salary_raw", "baseSalary", "salary")
TEXT_KEYS = ("salary", "salary_range", "salaryRange", "salary_text", "salaryText", "compensation", "pay")

SYMBOLS = {"US$": "USD", "S$": "SGD", "A$": "AUD", "C$": "CAD", "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
CODES = ("USD", "EUR", "GBP", "INR", "SGD", "AUD", "CAD", "AED", "CHF", "JPY", "NZD", "HKD")
PERIODS = (
    ("hour", r"\bhours?\b|\bhourly\b|/\s*hr\b|\bper\s+hr\b"),
    ("day", r"\bdays?\b|\bdaily\b"),
    ("week", r"\bweeks?\b|\bweekly\b"),
    ("month", r"\bmonths?\b|\bmonthly\b|/\s*mo\b"),
    ("year", r"\byears?\b|\byearly\b|\bannum\b|\bannual(?:ly)?\b|/\s*yr\b"),
)
AMOUNT = re.compile(r"(?P<cur>US\$|S\$|A\$|C\$|[$£€₹])?\s?"
                    r"(?P<num>\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)\s?(?P<suffix>[kKmM](?![A-Za-z]))?")
MULTIPLIER = {"": 1, "k": 1_000, "m": 1_000_000}


def _period(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    lowered = value.lower().replace("_", " ")
    return next((name for name, pattern in PERIODS if re.search(pattern, lowered)), None)


def _currency(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    clean = value.strip()
    if re.fullmatch(r"[A-Za-z]{3}", clean):
        return clean.upper()
    return SYMBOLS.get(clean)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*[^\d\s]{0,3}\s*(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)\s*([kKmM]?)\s*", value)
        if match:
            amount = float(match.group(1).replace(",", "")) * MULTIPLIER[match.group(2).lower()]
            return amount if amount > 0 else None
    return None


def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return next((key for key in keys if _number(data.get(key)) is not None), None)


def parse_salary_text(text: Any) -> dict[str, Any] | None:
    """Parse text such as '$120,000 - $150,000 a year' or '£55k-65k'. Returns None when unsure."""
    if not isinstance(text, str):
        return None
    values: list[list] = []
    currency = None
    for match in AMOUNT.finditer(text):
        values.append([float(match.group("num").replace(",", "")), (match.group("suffix") or "").lower()])
        currency = currency or SYMBOLS.get(match.group("cur") or "")
        if len(values) == 2:
            break
    if not values:
        return None
    if len(values) == 2 and values[1][1] and not values[0][1] and values[0][0] < 1000:
        values[0][1] = values[1][1]  # "120-150k": the suffix applies to both bounds
    amounts = [number * MULTIPLIER[suffix] for number, suffix in values]
    if not currency:
        code = re.search(rf"\b({'|'.join(CODES)})\b", text, re.I)
        currency = code.group(1).upper() if code else None
    low = min(amounts)
    high = max(amounts) if len(amounts) == 2 else None
    if currency is None and max(amounts) < 1000:
        return None  # "3 days a week" is not a salary
    return {"min": low, "max": high, "currency": currency, "period": _period(text)}


def _from_mapping(data: dict[str, Any]) -> dict[str, Any] | None:
    value = data["value"] if isinstance(data.get("value"), dict) else data
    low_key = _first_key(value, ("minValue", "min", "from", "minimum"))
    high_key = _first_key(value, ("maxValue", "max", "to", "maximum"))
    single_key = _first_key(value, ("value", "amount"))
    low = _number(value.get(low_key or single_key or "")) if (low_key or single_key) else None
    high = _number(value.get(high_key)) if high_key else None
    if low is None and high is None:
        return None
    return {"min": low if low is not None else high, "max": high if low is not None else None,
            "currency": _currency(data.get("currency")) or _currency(value.get("currency")),
            "period": _period(value.get("unitText")) or _period(data.get("unitText")) or _period(data.get("period"))
            or _period(data.get("interval"))}


def extract_posting_salary(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Salary stated by the posting itself, or None when the posting does not state one."""
    raw = raw if isinstance(raw, dict) else {}
    low_key, high_key = _first_key(raw, MIN_KEYS), _first_key(raw, MAX_KEYS)
    if low_key or high_key:
        low = _number(raw.get(low_key)) if low_key else None
        high = _number(raw.get(high_key)) if high_key else None
        return {"min": low if low is not None else high, "max": high if low is not None else None,
                "currency": next((c for key in CURRENCY_KEYS if (c := _currency(raw.get(key)))), None),
                "period": next((p for key in PERIOD_KEYS if (p := _period(raw.get(key)))), None),
                "field": low_key or high_key}
    for key in NESTED_KEYS:
        if isinstance(raw.get(key), dict) and (found := _from_mapping(raw[key])):
            return {**found, "field": key}
    for key in TEXT_KEYS:
        text = raw.get(key)
        if isinstance(text, str) and (found := parse_salary_text(text)):
            return {**found, "field": key, "text": text.strip()[:200]}
    return None


def enrich_compensation(raw: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    posting = extract_posting_salary(raw)
    return {"status": "found" if posting else "not_listed", "posting": posting,
            "glassdoor": {"status": "not_configured"}, "payscale": {"status": "not_configured"},
            "checked_at": now.isoformat()}


# ───────────── compensation intelligence (Job Insights) ─────────────
# Observations from several sources are compared, never blended blindly. The employer's own posting is the highest
# source and is never replaced by an estimate; a recruiter's stated budget comes next; market data (Glassdoor,
# PayScale, ...) only ever forms a *market estimate*, which is never presented as the employer's approved band.
#
# Rules kept here, deterministically (no model ever produces a salary):
# * Every observation keeps its original values (currency, period, basis); normalized annual values are added beside
#   them, in the same currency. There is no currency conversion: an observation in another currency is shown but left
#   out of the estimate, with the reason.
# * Base salary, total cash and total compensation are different things and are never compared with one another.
# * A pay period that is not stated stays unknown; such an observation is shown but not used for the estimate.
# * When credible estimates disagree by more than CONFLICT_THRESHOLD, the result is CONFLICTING_ESTIMATES and no
#   midpoint is made up: the alternatives are listed instead.

BASES = ("BASE", "TOTAL_CASH", "TOTAL_COMPENSATION", "UNKNOWN")
# Hours and days a year behind each annualization; recorded on every converted value so it can be checked.
PERIOD_FACTORS = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}
ANNUALIZATION_ASSUMPTION = "52 weeks, 260 working days or 2,080 working hours a year"
CONFLICT_THRESHOLD = 0.25
CREDIBLE_CONFIDENCE = 0.5
MARKET_SOURCES = ("GLASSDOOR", "PAYSCALE", "LEVELS_FYI", "INDEED", "PUBLIC_WEB")
COMPANY_SCOPES = ("COMPANY_ROLE_CITY", "COMPANY_ROLE_COUNTRY", "COMPANY_ROLE", "COMPANY_SIMILAR_ROLE")
STATUSES = ("EMPLOYER_POSTED", "RECRUITER_PROVIDED", "MULTI_SOURCE_CONSENSUS", "SINGLE_SOURCE_ESTIMATE",
            "CONFLICTING_ESTIMATES", "UNKNOWN")


def normalize_period(value: Any) -> str | None:
    """'ANNUAL', 'per hour', 'yearly' → year / hour / ...; None when the text names no period."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in PERIOD_FACTORS:
        return lowered
    if lowered in ("annual", "annually", "yr", "per_year", "peryear", "annum", "a"):
        return "year"
    if lowered in ("hr", "hourly", "per_hour"):
        return "hour"
    if lowered in ("monthly", "mo", "per_month"):
        return "month"
    return _period(value)


def normalize_basis(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "UNKNOWN"
    lowered = value.lower().replace("-", " ").replace("_", " ")
    if "total comp" in lowered or "total pay" in lowered or "tc" == lowered.strip() or "equity" in lowered:
        return "TOTAL_COMPENSATION"
    if "total cash" in lowered or "cash" in lowered or "bonus" in lowered:
        return "TOTAL_CASH"
    if "base" in lowered or "salary" in lowered:
        return "BASE"
    return "UNKNOWN"


def annualize(value: float | None, period: str | None) -> float | None:
    """The yearly figure for `value` in the same currency, or None when the period is unknown."""
    if value is None or period not in PERIOD_FACTORS:
        return None
    return round(float(value) * PERIOD_FACTORS[period], 2)


def _amount_range(data: dict[str, Any]) -> tuple[float | None, float | None]:
    low, high = _number(data.get("low")), _number(data.get("high"))
    median = _number(data.get("median"))
    if low is None and high is None and median is not None:
        return median, median
    if low is None:
        return high, high
    return low, high if high is not None else low


def observation_row(obs: dict[str, Any]) -> dict[str, Any]:
    """One compensation observation as the insight shows it: original values, then annual values beside them."""
    data = obs.get("data") or {}
    low, high = _amount_range(data)
    period = normalize_period(data.get("period"))
    currency = _currency(data.get("currency"))
    return {
        "id": obs.get("id"), "source_type": obs.get("source_type"), "source_name": obs.get("source_name"),
        "title": obs.get("title"), "match_scope": obs.get("match_scope"), "recency": obs.get("recency"),
        "confidence": obs.get("confidence"), "observed_at": obs.get("observed_at"),
        "original": {"low": low, "high": high, "median": _number(data.get("median")), "currency": currency,
                     "period": period, "text": (data.get("text") or "")[:200] or None},
        "currency": currency, "basis": normalize_basis(data.get("basis")),
        "annual_low": annualize(low, period), "annual_high": annualize(high, period),
        "sample_size": data.get("sample_size") if isinstance(data.get("sample_size"), int) else None,
        "included": False, "excluded_reason": None,
    }


def _mid(row: dict[str, Any]) -> float | None:
    if row["annual_low"] is None or row["annual_high"] is None:
        return None
    return (row["annual_low"] + row["annual_high"]) / 2


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values)
    total = sum(w for _, w in ordered)
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= total / 2:
            return value
    return ordered[-1][0]


def precedence(row: dict[str, Any]) -> int:
    """1 is the most authoritative source of a salary for this job; 8 the broadest market estimate."""
    source, scope = row.get("source_type"), row.get("match_scope") or ""
    if source in ("EMPLOYER", "JOB_DESCRIPTION"):
        return 1
    if source == "RECRUITER":
        return 2
    company = scope.startswith("COMPANY_ROLE")
    if source == "GLASSDOOR" and company:
        return 3
    if source == "PAYSCALE" and company:
        return 4
    if scope == "COMPANY_SIMILAR_ROLE":
        return 5
    if source == "GLASSDOOR" and scope.startswith("MARKET_ROLE"):
        return 6
    if source == "PAYSCALE" and scope.startswith("MARKET_ROLE"):
        return 7
    return 8


def _range_out(row: dict[str, Any], label: str) -> dict[str, Any]:
    original = row["original"]
    return {"low": original["low"], "high": original["high"], "currency": row["currency"],
            "period": original["period"], "basis": row["basis"], "annual_low": row["annual_low"],
            "annual_high": row["annual_high"], "source_ids": [row["id"]] if row["id"] is not None else [],
            "source_name": row["source_name"], "label": label}


def _confidence_label(score: float) -> str:
    return "HIGH" if score >= 0.75 else "MEDIUM" if score >= 0.5 else "LOW" if score > 0 else "NONE"


def compensation_insight(observations: list[dict[str, Any]], *, role_title: str = "", normalized_title: str = "",
                         company: str = "", location: str = "",
                         conflict_threshold: float = CONFLICT_THRESHOLD) -> dict[str, Any]:
    """Compare accepted compensation observations and say what they support. Deterministic; never invents a figure.

    `observations` are insight_observations rows (as dicts: id, source_type, source_name, match_scope, recency,
    confidence, observed_at, data{low, high, median, currency, period, basis, sample_size, text})."""
    rows = sorted((observation_row(o) for o in observations), key=lambda r: (precedence(r), -(r["confidence"] or 0)))
    employer = next((r for r in rows if r["source_type"] in ("EMPLOYER", "JOB_DESCRIPTION")
                     and r["original"]["low"] is not None), None)
    recruiter = next((r for r in rows if r["source_type"] == "RECRUITER" and r["original"]["low"] is not None), None)
    for r in (employer, recruiter):
        if r is not None:
            r["included"] = True
    market = [r for r in rows if r["source_type"] in MARKET_SOURCES and r["original"]["low"] is not None]

    # The currency and basis the estimate is made in: the employer's, else the recruiter's, else the most reported.
    anchor = employer or recruiter
    counted: dict[tuple[str | None, str], int] = {}
    for r in market:
        counted[(r["currency"], r["basis"])] = counted.get((r["currency"], r["basis"]), 0) + 1
    if anchor is not None and anchor["currency"]:
        currency = anchor["currency"]
        bases = [b for (c, b) in counted if c == currency]
        basis = anchor["basis"] if anchor["basis"] in bases else ("BASE" if "BASE" in bases else
                                                                    (bases[0] if bases else anchor["basis"]))
    elif counted:
        (currency, basis), _ = max(counted.items(), key=lambda item: (item[1], item[0][1] == "BASE"))
    else:
        currency, basis = (anchor or {}).get("currency"), (anchor or {}).get("basis", "UNKNOWN")

    usable: list[dict[str, Any]] = []
    for r in market:
        if r["currency"] is None:
            r["excluded_reason"] = "Currency not stated"
        elif r["currency"] != currency:
            r["excluded_reason"] = f"In {r['currency']}; not converted (no exchange-rate source is configured)"
        elif r["basis"] != basis:
            r["excluded_reason"] = f"{r['basis'].replace('_', ' ').lower()}, not {basis.replace('_', ' ').lower()}"
        elif _mid(r) is None:
            r["excluded_reason"] = "Pay period not stated"
        else:
            usable.append(r)

    notes: list[str] = []
    conflict = None
    credible = [r for r in usable if (r["confidence"] or 0) >= CREDIBLE_CONFIDENCE]
    mids = [_mid(r) for r in credible]
    if len(mids) >= 2 and min(mids) > 0 and (max(mids) - min(mids)) / min(mids) > conflict_threshold:
        conflict = {"threshold": conflict_threshold, "spread": round((max(mids) - min(mids)) / min(mids), 3),
                    "between": "market sources",
                    "alternatives": [_range_out(r, r["source_name"] or "") for r in credible]}

    market_range = None
    if usable and conflict is None:
        for r in usable:
            r["included"] = True
        weights = [max(0.05, r["confidence"] or 0.05) for r in usable]
        low = _weighted_median([(r["annual_low"], w) for r, w in zip(usable, weights, strict=True)])
        high = _weighted_median([(r["annual_high"], w) for r, w in zip(usable, weights, strict=True)])
        low, high = min(low, high), max(low, high)
        sources = {r["source_name"] for r in usable}
        samples = [r["sample_size"] for r in usable if r["sample_size"]]
        score = min(1.0, sum(weights) / len(weights) + (0.1 if len(sources) > 1 else -0.1))
        market_range = {"low": round(low), "midpoint": round((low + high) / 2), "high": round(high),
                        "currency": currency, "period": "year", "basis": basis,
                        "source_ids": [r["id"] for r in usable], "sources": sorted(s for s in sources if s),
                        "sample_size": sum(samples) if samples else None, "confidence": _confidence_label(score),
                        "confidence_score": round(max(0.0, score), 2),
                        "method": "Weighted median of the annual low and high of each matching observation "
                                  "(weights: match quality × recency × source confidence)",
                        "label": "Market estimate — not the employer's approved band"}
        # Compared only like for like: same currency and the same known basis. A posting that does not say whether
        # its range is base or total pay is never measured against market base salary.
        if employer is not None and _mid(employer) and market_range["midpoint"] and employer["currency"] == currency \
                and employer["basis"] == basis and basis != "UNKNOWN":
            gap = abs(market_range["midpoint"] - _mid(employer)) / min(market_range["midpoint"], _mid(employer))
            if gap > conflict_threshold:
                conflict = {"threshold": conflict_threshold, "spread": round(gap, 3), "between": "employer and market",
                            "alternatives": [_range_out(employer, "Employer posting"),
                                             {**market_range, "label": "Market estimate"}]}
                notes.append("The employer's posted range differs from market data; the employer's range stands.")

    if conflict is not None:
        status = "CONFLICTING_ESTIMATES"
    elif employer is not None:
        status = "EMPLOYER_POSTED"
    elif recruiter is not None:
        status = "RECRUITER_PROVIDED"
    elif market_range is not None:
        status = "MULTI_SOURCE_CONSENSUS" if len(market_range["sources"]) > 1 else "SINGLE_SOURCE_ESTIMATE"
    else:
        status = "UNKNOWN"

    if employer is not None:
        discussion = _range_out(employer, "The employer's posted range")
    elif recruiter is not None:
        discussion = _range_out(recruiter, "The recruiter's stated budget")
    elif market_range is not None:
        discussion = {**market_range, "label": "Market estimate — ask the recruiter for the approved band"}
    else:
        discussion = None

    def by_basis(wanted: str) -> dict[str, Any] | None:
        if market_range is not None and basis == wanted:
            return {k: market_range[k] for k in ("low", "midpoint", "high", "currency", "period")}
        if employer is not None and employer["basis"] == wanted and employer["annual_low"] is not None:
            low_, high_ = employer["annual_low"], employer["annual_high"] or employer["annual_low"]
            return {"low": low_, "midpoint": (low_ + high_) / 2, "high": high_, "currency": employer["currency"],
                    "period": "year"}
        return None

    dates = sorted(str(r["observed_at"]) for r in rows if r.get("observed_at"))
    confidence = ("HIGH" if employer is not None else "HIGH" if recruiter is not None
                  else market_range["confidence"] if market_range else "NONE")
    if employer is None:
        notes.append("The posting does not state a salary.")
    if employer is not None and employer["basis"] == "UNKNOWN":
        notes.append("The posting does not say whether its range is base salary or total compensation.")
    return {
        "status": status, "role_title": role_title, "normalized_role_title": normalized_title, "company": company,
        "location": location, "currency": currency, "period": "year", "basis": basis,
        "employer_posted_range": _range_out(employer, "Employer posting") if employer else None,
        "recruiter_budget": _range_out(recruiter, "Recruiter") if recruiter else None,
        "market_range": market_range, "base_salary": by_basis("BASE"), "total_cash": by_basis("TOTAL_CASH"),
        "total_compensation": by_basis("TOTAL_COMPENSATION"),
        "recommended_discussion_range": discussion, "confidence": confidence,
        "sample_size": (market_range or {}).get("sample_size"),
        "date_range": {"from": dates[0], "to": dates[-1]} if dates else None,
        "conflict": conflict, "observations": rows, "notes": notes,
        "annualization": ANNUALIZATION_ASSUMPTION,
    }
