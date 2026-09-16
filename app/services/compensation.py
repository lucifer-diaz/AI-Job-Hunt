"""Salary enrichment from the original job posting.

Deterministic and conservative: structured salary fields that job-board actors return are used
first, then explicit salary text fields. The free-text description is never mined, because numbers
in prose (team sizes, years of experience, revenue) are too easy to misread as pay.

Glassdoor and PayScale are separate sources that are not integrated yet. They are recorded as
`not_configured` so a later enricher can fill them in without re-reading the posting.
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
