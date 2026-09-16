"""Apify actor adapters for the Hunter.

Actors disagree on input field names, on how many searches one run covers, on which filters exist
and on what their dataset items look like. Each actor therefore gets its own adapter, and nothing
outside this module knows an actor's field names:

    build_input(search, roles, location, sites)   the actor input, using only that actor's schema fields
    normalize_result(item, context)               one dataset item -> the internal job fields, or a skip reason
    check_schema(items)                           whether the fields this adapter relies on are still there

Input fields were checked against each actor's published input schema; the output fields against
real datasets (multi-board actor) and the actors' documented output (the fallbacks).

Actor IDs come from settings (APIFY_*_ACTOR), never from the business logic.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.services.compensation import parse_salary_text
from app.settings import get_settings

HUNTER_SITES = ("linkedin", "indeed", "glassdoor", "zip_recruiter", "bayt", "naukri")
DEFAULT_SITES = ("linkedin", "indeed", "glassdoor")
JOB_TYPES = ("", "fulltime", "parttime", "contract", "internship", "temporary")
MODES = ("primary", "fallback")
MAX_ROLES = 25
MAX_LOCATIONS = 10
SKIP_REASONS = ("missing_title", "missing_company", "expired")


class AdapterError(ValueError):
    """An actor input that the actor's schema does not accept, or an unknown adapter."""


class SchemaMismatch(RuntimeError):
    """A dataset whose items no longer carry the fields an adapter relies on."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def clean_list(values: Any) -> list[str]:
    """Trim, drop blanks and case-insensitive duplicates, keep order."""
    out, seen = [], set()
    for value in values or []:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


@dataclass(frozen=True)
class HunterSearch:
    roles: tuple[str, ...]
    locations: tuple[str, ...]
    sites: tuple[str, ...] = DEFAULT_SITES
    hours_old: int | None = 72
    max_results: int = 20
    remote_only: bool = False
    job_type: str = ""
    country: str = ""
    mode: str = "primary"
    require_posted_date: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HunterSearch:
        sites = [s.lower() for s in clean_list(data.get("sites"))]
        return cls(roles=tuple(clean_list(data.get("roles"))), locations=tuple(clean_list(data.get("locations"))),
                   sites=tuple(sites) if data.get("sites") is not None else DEFAULT_SITES,
                   hours_old=int(data["hours_old"]) if data.get("hours_old") else None,
                   max_results=int(data.get("max_results") or 20), remote_only=bool(data.get("remote_only")),
                   job_type=str(data.get("job_type") or ""), country=str(data.get("country") or "").strip(),
                   mode=str(data.get("mode") or "primary"), require_posted_date=bool(data.get("require_posted_date")))

    def params(self) -> dict[str, Any]:
        """The filters stored with each run, used again when its results are imported."""
        return {"hours_old": self.hours_old, "max_results": self.max_results, "remote_only": self.remote_only,
                "job_type": self.job_type, "country": self.country, "mode": self.mode,
                "require_posted_date": self.require_posted_date}

    def problems(self) -> list[str]:
        found = []
        for label, values, limit in (("role", self.roles, MAX_ROLES), ("location", self.locations, MAX_LOCATIONS)):
            if not values:
                found.append(f"Add at least one {label}")
            if len(values) > limit:
                found.append(f"Use at most {limit} {label}s")
            if any(len(v) > 120 for v in values):
                found.append(f"Each {label} must be 120 characters or fewer")
        unknown = [s for s in self.sites if s not in HUNTER_SITES]
        if unknown:
            found.append(f"Unknown sites: {', '.join(unknown)}; choose from {', '.join(HUNTER_SITES)}")
        if not self.sites:
            found.append("Choose at least one site")
        if self.hours_old is not None and not 1 <= self.hours_old <= 8760:
            found.append("hours_old must be between 1 and 8760")
        if not 1 <= self.max_results <= 100:
            found.append("max_results must be between 1 and 100")
        if self.job_type not in JOB_TYPES:
            found.append(f"Unknown job type; choose from {', '.join(t for t in JOB_TYPES if t)}")
        if self.mode not in MODES:
            found.append("mode must be primary or fallback")
        return found


@dataclass(frozen=True)
class PlannedRun:
    adapter: str
    actor_id: str
    roles: list[str]
    location: str
    sites: list[str]
    input: dict[str, Any]


@dataclass(frozen=True)
class RunContext:
    """What a dataset item was searched for, so every job records why it was found."""

    roles: tuple[str, ...]
    location: str
    actor_id: str
    run_id: str
    reference_time: datetime | None  # the run's finish (or start) time, for relative posting dates
    search_profile_id: int | None = None


# ───────────── countries ─────────────

@dataclass(frozen=True)
class Country:
    indeed_name: str  # multi-board actor countryIndeed
    linkedin: str     # thirdwatch LinkedIn country enum ("" when not offered)
    iso: str          # crawlerbros Indeed country enum


COUNTRIES: list[tuple[tuple[str, ...], Country]] = [
    (("singapore",), Country("singapore", "singapore", "SG")),
    (("united arab emirates", "uae", "dubai", "abu dhabi", "sharjah"), Country("uae", "uae", "AE")),
    (("india", "bangalore", "bengaluru", "mumbai", "pune", "hyderabad", "chennai", "delhi", "gurgaon", "gurugram",
      "noida", "kolkata"), Country("india", "india", "IN")),
    (("united kingdom", "uk", "england", "london", "manchester", "edinburgh"), Country("uk", "uk", "GB")),
    (("united states", "usa", "us", "new york", "san francisco", "seattle", "austin", "boston"), Country("usa", "us", "US")),
    (("canada", "toronto", "vancouver", "montreal"), Country("canada", "canada", "CA")),
    (("australia", "sydney", "melbourne"), Country("australia", "australia", "AU")),
    (("germany", "berlin", "munich"), Country("germany", "germany", "DE")),
    (("netherlands", "amsterdam"), Country("netherlands", "netherlands", "NL")),
    (("ireland", "dublin"), Country("ireland", "ireland", "IE")),
    (("france", "paris"), Country("france", "france", "FR")),
    (("japan", "tokyo"), Country("japan", "japan", "JP")),
    (("saudi arabia", "riyadh", "jeddah"), Country("saudi arabia", "", "SA")),
    (("qatar", "doha"), Country("qatar", "", "QA")),
]


def resolve_country(explicit: str, location: str) -> Country | None:
    """The country of a search: the explicit country when given, otherwise recognised from the location."""
    for text in (explicit, location):
        lowered = (text or "").lower()
        for words, country in COUNTRIES:
            if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
                return country
    return None


# ───────────── value helpers ─────────────

def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, dict):
        return _text(_first(value, ("name", "text", "title", "formatted", "value")))
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    return str(value).strip()


def _cut(value: Any, length: int) -> str | None:
    text = _text(value)
    return text[:length] if text else None


def _plain(value: Any) -> str:
    text = _text(value)
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    return text


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    lowered = _text(value).lower()
    return True if lowered in ("true", "yes", "1") else False if lowered in ("false", "no", "0") else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = re.fullmatch(r"\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*", str(value))
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    return number if number > 0 else None


def _url(value: Any) -> str | None:
    text = _text(value)
    return text[:1200] if text.lower().startswith(("http://", "https://")) else None


def _skills(value: Any) -> list[str] | None:
    parts = value if isinstance(value, list) else re.split(r"[,;|]", _text(value))
    return clean_list(_text(p) for p in parts)[:50] or None


def _interval(value: Any) -> str | None:
    lowered = _text(value).lower()
    for pattern, name in ((r"year|annual|annum|\bpa\b", "yearly"), (r"month", "monthly"), (r"week", "weekly"),
                          (r"day|daily", "daily"), (r"hour", "hourly")):
        if re.search(pattern, lowered):
            return name
    return None


def _remote(*values: Any) -> str | None:
    """'remote' or 'hybrid' only when the posting says so; otherwise unknown."""
    for value in values:
        if value is True or _bool(value) is True:
            return "remote"
        lowered = _text(value).lower()
        if "hybrid" in lowered:
            return "hybrid"
        if "remote" in lowered or "work from home" in lowered:
            return "remote"
    return None


def _salary_from_text(value: Any) -> dict[str, Any]:
    parsed = parse_salary_text(_text(value)) if _text(value) else None
    if not parsed:
        return {}
    return {"salary_min": parsed["min"], "salary_max": parsed["max"], "salary_currency": parsed["currency"],
            "salary_interval": _interval(parsed["period"]), "salary_source": "posting_text"}


def match_role(value: Any, roles: tuple[str, ...]) -> str | None:
    """The searched role an item was found by: reported by the actor, or the only role of the run. Never guessed."""
    text = _text(value)
    if text:
        return next((role for role in roles if role.lower() == text.lower()), text)
    return roles[0] if len(roles) == 1 else None


# ───────────── posting dates ─────────────

@dataclass(frozen=True)
class PostedDate:
    at: datetime | None       # naive UTC
    raw: str | None           # the value the actor returned
    precision: str | None     # "date" or "datetime"


RELATIVE = re.compile(r"^(?:posted|active|reposted)?\s*(?P<n>\d+|an?|one)\s*(?P<plus>\+)?\s*"
                      r"(?P<unit>minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w|months?|mos?|years?|yrs?|y)\s+ago$")
DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y")


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _midnight(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_posted_date(value: Any, reference: datetime | None) -> PostedDate:
    """Normalize a posting date without guessing.

    Absolute dates and timestamps are parsed as given. Relative values ("2 hours ago", "3 days ago",
    "yesterday") are counted back from `reference`, the time the actor scraped the page. Values that are
    not exact ("30+ days ago", "2 months ago") or unreadable give `at=None`. The raw value is always kept.
    """
    raw = None if value in (None, "") or isinstance(value, bool) else str(value)[:200]
    unknown = PostedDate(None, raw, None)
    if raw is None:
        return unknown
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", raw):
        number = float(value)
        seconds = number / 1000 if number > 1e11 else number if number > 1e9 else None
        return PostedDate(datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None), raw, "datetime") if seconds else unknown
    text = raw.strip()
    lowered = re.sub(r"\s+", " ", text.lower())
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lowered):
            return PostedDate(datetime.strptime(lowered, "%Y-%m-%d"), raw, "date")
        if re.match(r"\d{4}-\d{2}-\d{2}[t ]\d", lowered):
            return PostedDate(_naive_utc(datetime.fromisoformat(text.replace("Z", "+00:00"))), raw, "datetime")
    except ValueError:
        return unknown
    if reference is not None:
        if lowered in ("just now", "just posted", "moments ago"):
            return PostedDate(reference, raw, "datetime")
        if lowered in ("today", "posted today"):
            return PostedDate(_midnight(reference), raw, "date")
        if lowered in ("yesterday", "posted yesterday"):
            return PostedDate(_midnight(reference - timedelta(days=1)), raw, "date")
        match = RELATIVE.match(lowered)
        if match and not match.group("plus"):
            unit = match.group("unit")
            if unit.startswith(("mo", "y")):
                return unknown  # months and years are not exact enough to date a posting
            count = 1 if match.group("n") in ("a", "an", "one") else int(match.group("n"))
            if unit[0] in "mh":
                delta = timedelta(minutes=count) if unit[0] == "m" else timedelta(hours=count)
                return PostedDate(reference - delta, raw, "datetime")
            delta = timedelta(days=count) if unit[0] == "d" else timedelta(weeks=count)
            return PostedDate(_midnight(reference - delta), raw, "date")
    for fmt in DATE_FORMATS:
        try:
            return PostedDate(datetime.strptime(text, fmt), raw, "date")
        except ValueError:
            continue
    return unknown


def days_ago(days: Any, reference: datetime | None, raw: str) -> PostedDate:
    count = _float(days) if not isinstance(days, int) else float(days)
    if days in (0, "0"):
        count = 0.0
    if count is None or reference is None:
        return PostedDate(None, raw, None)
    return PostedDate(_midnight(reference - timedelta(days=int(count))), raw, "date")


def parse_timestamp(value: Any) -> datetime | None:
    return parse_posted_date(value, None).at


def older_than(posted_at: datetime | None, precision: str | None, reference: datetime | None,
               hours_old: int | None) -> bool:
    """Whether a known posting date falls outside the recency window. Unknown dates are never too old."""
    if posted_at is None or reference is None or not hours_old:
        return False
    cutoff = reference - timedelta(hours=hours_old)
    return posted_at.date() < cutoff.date() if precision == "date" else posted_at < cutoff


# ───────────── identity ─────────────

TRACKING_PARAM = re.compile(r"^(utm_|trk|refid$|ref$|src$|source$|from$|clickid|gclid$|fbclid$|mc_)", re.I)
URL_IDS = {
    "linkedin": re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})", re.I),
    "indeed": re.compile(r"indeed\.[a-z.]+/.*[?&](?:jk|vjk)=([0-9a-f]{16})", re.I),
    "glassdoor": re.compile(r"glassdoor\.[a-z.]+/.*[?&](?:jl|jobListingId)=(\d+)", re.I),
    "naukri": re.compile(r"naukri\.com/job-listings-.*?-(\d{9,})", re.I),
    "bayt": re.compile(r"bayt\.com/.*-(\d{5,})/?$", re.I),
}
# The multi-board actor prefixes board IDs (li-4464856182, in-921a4b7d05062cc5, nk-…, bayt-…).
ID_PREFIXES = {"linkedin": "li-", "indeed": "in-", "naukri": "nk-", "bayt": "bayt-", "glassdoor": "gd-",
               "zip_recruiter": "zr-"}
COMPANY_SUFFIXES = re.compile(r"\b(incorporated|inc|llc|ltd|limited|pvt|private|plc|gmbh|corp|corporation|pte)\b")


def canonical_url(url: str | None) -> str | None:
    """Same posting, same string: https, no www, no fragment, no trailing slash, no tracking parameters."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    query = sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False) if not TRACKING_PARAM.match(k))
    return urlunparse(("https", host, parsed.path.rstrip("/") or "/", "", urlencode(query), ""))[:1200]


def native_job_id(board: str, raw_id: Any, urls: tuple[str | None, ...]) -> str | None:
    """The board's own job ID, so the same posting matches across actors (from its URL first, then the item ID)."""
    pattern = URL_IDS.get(board)
    for url in urls:
        match = pattern.search(url) if pattern and url else None
        if match:
            return match.group(1)
    text = _text(raw_id)
    prefix = ID_PREFIXES.get(board)
    if prefix and text.lower().startswith(prefix):
        text = text[len(prefix):]
    return text[:200] or None


def dedupe_key(title: str, company: str, location: str = "") -> str:
    """Company + title + city, normalized, so one job found on several boards is recognised."""
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    company_name = re.sub(r"\s+", " ", COMPANY_SUFFIXES.sub(" ", norm(company))).strip()
    return f"{company_name}|{norm(title)}|{norm((location or '').split(',')[0])}"[:700]


# ───────────── adapters ─────────────

class ApifyActorAdapter:
    key = ""
    name = ""
    actor_setting = ""
    sites: tuple[str, ...] = ()
    input_fields: frozenset[str] = frozenset()
    roles_per_run = 1
    recency = "none"  # exact: the actor filters precisely; coarse: a wider window; none: no filter
    default_board = ""
    title_keys: tuple[str, ...] = ("title",)
    company_keys: tuple[str, ...] = ("company",)
    identity_keys: tuple[str, ...] = ("id", "url")
    scraped_keys: tuple[str, ...] = ("scraped_at", "scrapedAt")

    @property
    def actor_id(self) -> str:
        return getattr(get_settings(), self.actor_setting)

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "actor_id": self.actor_id, "sites": list(self.sites),
                "roles_per_run": self.roles_per_run, "recency_filter": self.recency,
                "input_fields": sorted(self.input_fields)}

    # input
    def plan(self, search: HunterSearch, sites: list[str]) -> tuple[list[PlannedRun], list[str]]:
        batches = [list(search.roles[i:i + self.roles_per_run]) for i in range(0, len(search.roles), self.roles_per_run)]
        runs = [PlannedRun(self.key, self.actor_id, roles, location, list(sites), self.payload(search, roles, location, sites))
                for location in search.locations for roles in batches]
        return runs, self.warnings(search, sites)

    def payload(self, search: HunterSearch, roles: list[str], location: str, sites: list[str]) -> dict[str, Any]:
        data = {k: v for k, v in self.build_input(search, roles, location, sites).items() if v is not None}
        unsupported = sorted(set(data) - self.input_fields)
        if unsupported:
            raise AdapterError(f"{self.name}: {', '.join(unsupported)} is not in the actor's input schema")
        return data

    def build_input(self, search: HunterSearch, roles: list[str], location: str, sites: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    def warnings(self, search: HunterSearch, sites: list[str]) -> list[str]:
        return []

    # output
    def check_schema(self, items: list[Any]) -> list[str]:
        """Problems when a field this adapter needs is absent from every item of a dataset with several items."""
        objects = [item for item in items if isinstance(item, dict)]
        if items and not objects:
            return ["dataset items are not JSON objects"]
        if len(objects) < 2:
            return []  # a single odd item is skipped on its own rather than failing the run
        problems = []
        for label, keys in (("a title", self.title_keys), ("a company", self.company_keys),
                            ("a job ID or URL", self.identity_keys)):
            if not any(_first(item, keys) is not None for item in objects):
                problems.append(f"no item has {label} (expected one of: {', '.join(keys)})")
        return problems

    def fields(self, item: dict[str, Any], ctx: RunContext, reference: datetime | None) -> dict[str, Any]:
        raise NotImplementedError

    def normalize_result(self, item: dict[str, Any], ctx: RunContext) -> dict[str, Any] | str:
        """Internal job fields for one dataset item, or a skip reason from SKIP_REASONS."""
        scraped = parse_timestamp(_first(item, self.scraped_keys))
        reference = scraped or ctx.reference_time
        found = self.fields(item, ctx, reference)
        if found.get("expired"):
            return "expired"
        title, company = _cut(found.get("title"), 300), _cut(found.get("company"), 300)
        if not title:
            return "missing_title"
        if not company:
            return "missing_company"
        board = (_cut(found.get("board"), 50) or self.default_board).lower().replace(" ", "_")
        job_url, apply_url = _url(found.get("job_url")), _url(found.get("direct_apply_url"))
        posted: PostedDate = found.get("posted") or PostedDate(None, None, None)
        location = _cut(found.get("location"), 300) or ""
        return {
            "source_board": board, "source_job_id": native_job_id(board, found.get("raw_id"), (job_url, apply_url)),
            "title": title, "company": company, "location": location,
            "description": (found.get("description") or "")[:100000],
            "job_url": job_url, "direct_apply_url": apply_url,
            "canonical_url": canonical_url(job_url), "canonical_apply_url": canonical_url(apply_url),
            "dedupe_key": dedupe_key(title, company, location),
            "posted_at": posted.at, "posted_date_raw": _cut(posted.raw, 200), "posted_precision": posted.precision,
            "scraped_at": scraped, "job_type": _cut(found.get("job_type"), 50), "remote_type": found.get("remote_type"),
            "seniority": _cut(found.get("seniority"), 100), "skills": found.get("skills") or None,
            "experience": _cut(found.get("experience"), 200),
            "salary_min": found.get("salary_min"), "salary_max": found.get("salary_max"),
            "salary_currency": (_cut(found.get("salary_currency"), 10) or "").upper() or None,
            "salary_interval": found.get("salary_interval"), "salary_source": _cut(found.get("salary_source"), 50),
            "matched_search_role": _cut(match_role(found.get("matched_role"), ctx.roles), 300),
            "raw": item,
        }


class MultiBoardAdapter(ApifyActorAdapter):
    """openclawai/job-board-scraper: up to 5 roles and one location per run, several boards at once."""

    key, name, actor_setting = "multi_board", "Multi Job Board", "apify_primary_job_actor"
    sites = HUNTER_SITES
    input_fields = frozenset({"searchTerm", "searchTerms", "location", "sites", "maxResults", "hoursOld", "isRemote",
                              "countryIndeed", "distance", "linkedinFetchDescription", "enforceAnnualSalary", "jobType"})
    roles_per_run, recency = 5, "exact"
    identity_keys = ("id", "job_url", "job_url_direct")

    def build_input(self, search, roles, location, sites):
        country = resolve_country(search.country, location)
        # searchTerm is the actor's single-query field; searchTerms (max 5) runs every role in the batch.
        return {"searchTerm": roles[0], "searchTerms": roles, "location": location, "sites": sites,
                "maxResults": search.max_results, "hoursOld": search.hours_old, "isRemote": search.remote_only,
                "countryIndeed": country.indeed_name if country else None, "distance": 50,
                "linkedinFetchDescription": True, "enforceAnnualSalary": True, "jobType": search.job_type or None}

    def warnings(self, search, sites):
        notes = []
        if search.hours_old and (search.remote_only or search.job_type) and "indeed" in sites:
            notes.append("Multi Job Board: Indeed ignores remote-only and job type when hours_old is set.")
        if {"indeed", "glassdoor"} & set(sites):
            for location in search.locations:
                if not resolve_country(search.country, location):
                    notes.append(f"Multi Job Board: could not tell the country of '{location}', so Indeed and "
                                 "Glassdoor search the actor's default country (USA). Set country to fix this.")
        return notes

    def fields(self, item, ctx, reference):
        return {
            "title": item.get("title"), "company": item.get("company"), "location": item.get("location"),
            "description": _plain(item.get("description")), "job_url": item.get("job_url"),
            "direct_apply_url": item.get("job_url_direct"), "board": item.get("site"), "raw_id": item.get("id"),
            "posted": parse_posted_date(item.get("date_posted"), reference), "job_type": item.get("job_type"),
            "remote_type": _remote(item.get("is_remote"), item.get("work_from_home_type")),
            "seniority": item.get("job_level"), "skills": _skills(item.get("skills")),
            "experience": item.get("experience_range"),
            "salary_min": _float(item.get("salary_min")), "salary_max": _float(item.get("salary_max")),
            "salary_currency": item.get("salary_currency"), "salary_interval": _interval(item.get("salary_interval")),
            "salary_source": item.get("salary_source"),
            "matched_role": _first(item, ("matched_search_term", "search_term")),
        }


LINKEDIN_WINDOWS = ((24, "past24h"), (168, "pastWeek"), (720, "pastMonth"))
LINKEDIN_JOB_TYPES = {"fulltime": "F", "parttime": "P", "contract": "C", "temporary": "T", "internship": "I"}


class LinkedInAdapter(ApifyActorAdapter):
    """thirdwatch/linkedin-jobs-scraper: several queries and one location per run."""

    key, name, actor_setting = "linkedin", "LinkedIn Jobs", "apify_linkedin_actor"
    sites, default_board = ("linkedin",), "linkedin"
    input_fields = frozenset({"queries", "location", "country", "companyName", "maxResultsPerQuery", "datePosted",
                              "jobType", "experienceLevel"})
    roles_per_run, recency = 5, "coarse"
    title_keys, company_keys = ("title", "job_title"), ("company_name", "company")
    identity_keys = ("job_url", "url", "link", "apply_url", "job_id", "id")

    def build_input(self, search, roles, location, sites):
        country = resolve_country(search.country, location)
        window = next((name for hours, name in LINKEDIN_WINDOWS if search.hours_old and search.hours_old <= hours), None)
        return {"queries": roles, "location": location, "country": (country.linkedin or None) if country else None,
                "maxResultsPerQuery": search.max_results, "datePosted": window,
                "jobType": LINKEDIN_JOB_TYPES.get(search.job_type)}

    def warnings(self, search, sites):
        notes = []
        if search.hours_old and search.hours_old > 720:
            notes.append("LinkedIn fallback: its date filter stops at 30 days, so older postings are filtered after import.")
        if search.remote_only:
            notes.append("LinkedIn fallback: the actor has no remote-only filter, so remote-only is not applied.")
        return notes

    def fields(self, item, ctx, reference):
        apply_url = _text(item.get("apply_url"))
        job_url = _first(item, ("job_url", "url", "link", "linkedin_url")) or apply_url
        salary = {"salary_min": _float(item.get("salary_min")), "salary_max": _float(item.get("salary_max")),
                  "salary_currency": item.get("salary_currency"),
                  "salary_interval": _interval(_first(item, ("salary_period", "salary_interval"))),
                  "salary_source": "posting" if _float(item.get("salary_min")) or _float(item.get("salary_max")) else None}
        if not salary["salary_source"]:
            salary = _salary_from_text(item.get("salary_raw")) or salary
        return {
            "title": _first(item, self.title_keys), "company": _first(item, self.company_keys),
            "location": item.get("location"), "description": _plain(item.get("description")),
            "job_url": job_url, "direct_apply_url": apply_url if apply_url and "linkedin.com" not in apply_url.lower() else None,
            "raw_id": _first(item, ("job_id", "id")),
            "posted": parse_posted_date(_first(item, ("posted_at", "posted_date", "date_posted", "postedAt")), reference),
            "job_type": item.get("job_type"), "remote_type": _remote(item.get("work_type"), item.get("workplace_type")),
            "seniority": item.get("experience_level"), "skills": _skills(item.get("skills")),
            "matched_role": _first(item, ("query", "search_query", "searchQuery", "keyword")), **salary,
        }


class IndeedAdapter(ApifyActorAdapter):
    """crawlerbros/indeed-jobs-scraper: one position and one location per run."""

    key, name, actor_setting = "indeed", "Indeed Jobs", "apify_indeed_actor"
    sites, default_board = ("indeed",), "indeed"
    input_fields = frozenset({"position", "location", "country", "maxItemsPerSearch", "parseCompanyDetails",
                              "saveOnlyUniqueItems", "followApplyRedirects"})
    title_keys, company_keys = ("positionName", "title"), ("company", "companyName")
    identity_keys = ("id", "url")

    def build_input(self, search, roles, location, sites):
        country = resolve_country(search.country, location)
        return {"position": roles[0], "location": location, "country": country.iso if country else None,
                "maxItemsPerSearch": search.max_results, "parseCompanyDetails": False, "saveOnlyUniqueItems": True,
                "followApplyRedirects": True}

    def warnings(self, search, sites):
        notes = ["Indeed fallback: the actor has no date filter, so postings older than hours_old are dropped after "
                 "import when their date is known."] if search.hours_old else []
        if search.remote_only or search.job_type:
            notes.append("Indeed fallback: the actor cannot filter by remote-only or job type.")
        notes += [f"Indeed fallback: could not tell the country of '{loc}', so it searches Indeed US."
                  for loc in search.locations if not resolve_country(search.country, loc)]
        return notes

    def fields(self, item, ctx, reference):
        posted = parse_posted_date(item.get("postingDateParsed"), reference)
        if posted.at is None:
            posted = parse_posted_date(item.get("postedAt"), reference)
        elif item.get("postedAt"):
            posted = PostedDate(posted.at, str(item["postedAt"])[:200], posted.precision)  # keep the original wording
        return {
            "title": _first(item, self.title_keys), "company": _first(item, self.company_keys),
            "location": item.get("location"), "description": _plain(item.get("description")),
            "job_url": item.get("url"), "direct_apply_url": item.get("externalApplyLink"), "raw_id": item.get("id"),
            "posted": posted, "job_type": _text(item.get("jobType")) or None, "remote_type": _remote(item.get("location")),
            "expired": _bool(item.get("isExpired")) is True, **_salary_from_text(item.get("salary")),
        }


class GlassdoorAdapter(ApifyActorAdapter):
    """crawlerbros/glassdoor-jobs-scraper: one keyword and one location per run."""

    key, name, actor_setting = "glassdoor", "Glassdoor Jobs", "apify_glassdoor_actor"
    sites, default_board = ("glassdoor",), "glassdoor"
    input_fields = frozenset({"keyword", "location", "maxItems", "companyUrl"})
    title_keys, company_keys = ("jobTitle", "title"), ("companyName", "employerName", "company")
    identity_keys = ("jobListingId", "jobUrl", "url")

    def build_input(self, search, roles, location, sites):
        # companyUrl is for company-specific searches only; Hunter searches use keyword + location.
        return {"keyword": roles[0], "location": location, "maxItems": search.max_results}

    def warnings(self, search, sites):
        notes = ["Glassdoor fallback: the actor has no date filter, so postings older than hours_old are dropped "
                 "after import when their age is known."] if search.hours_old else []
        if search.remote_only or search.job_type:
            notes.append("Glassdoor fallback: the actor cannot filter by remote-only or job type.")
        return notes

    def fields(self, item, ctx, reference):
        age = item.get("ageInDays")
        posted = (days_ago(age, reference, f"ageInDays: {age}") if age not in (None, "")
                  else parse_posted_date(_first(item, ("postedDate", "datePosted")), reference))
        low, high = _float(item.get("salary_min")), _float(item.get("salary_max"))
        return {
            "title": _first(item, self.title_keys), "company": _first(item, self.company_keys),
            "location": item.get("location"), "description": _plain(item.get("description")),
            "job_url": _first(item, ("jobUrl", "url")), "raw_id": item.get("jobListingId"), "posted": posted,
            "remote_type": _remote(item.get("location")), "skills": _skills(item.get("skills")),
            "salary_min": low, "salary_max": high, "salary_currency": item.get("salaryCurrency"),
            "salary_interval": _interval(item.get("salaryPeriod")),
            "salary_source": _text(item.get("salarySource")) or ("glassdoor_estimate" if low or high else None),
        }


NAUKRI_JOB_AGES = (1, 3, 7, 15, 30)
NAUKRI_JOB_TYPES = {"fulltime": "full-time", "parttime": "part-time", "internship": "internship", "contract": "contractual"}


class NaukriAdapter(ApifyActorAdapter):
    """crawlerbros/naukri-scraper: one keyword and one city per run."""

    key, name, actor_setting = "naukri", "Naukri", "apify_naukri_actor"
    sites, default_board = ("naukri",), "naukri"
    input_fields = frozenset({"keyword", "location", "maxItems", "experienceYears", "salaryLakhs", "jobAge", "jobType",
                              "workMode", "sortBy"})
    recency = "coarse"
    title_keys, company_keys = ("title", "jobTitle"), ("companyName", "company")
    identity_keys = ("id", "url", "jdURL")

    def build_input(self, search, roles, location, sites):
        days = math.ceil(search.hours_old / 24) if search.hours_old else None
        age = next((str(d) for d in NAUKRI_JOB_AGES if days and days <= d), None)
        city = re.sub(r"\s+", "-", location.split(",")[0].strip().lower())
        return {"keyword": roles[0], "location": city, "maxItems": search.max_results, "jobAge": age,
                "jobType": NAUKRI_JOB_TYPES.get(search.job_type), "workMode": "remote" if search.remote_only else None,
                "sortBy": "date"}

    def warnings(self, search, sites):
        notes = []
        if search.job_type == "temporary":
            notes.append("Naukri fallback: there is no temporary job type, so job type is not applied.")
        for location in search.locations:
            country = resolve_country(search.country, location)
            if country and country.iso != "IN":
                notes.append(f"Naukri fallback: Naukri lists jobs in India, so '{location}' may return nothing.")
        return notes

    def fields(self, item, ctx, reference):
        return {
            "title": _first(item, self.title_keys), "company": _first(item, self.company_keys),
            "location": item.get("location"), "description": _plain(_first(item, ("description", "jobDescription"))),
            "job_url": _first(item, ("url", "jdURL")), "raw_id": item.get("id"),
            "posted": parse_posted_date(_first(item, ("postedDate", "datePosted", "createdDate")), reference),
            "remote_type": _remote(item.get("workMode"), item.get("location")), "skills": _skills(item.get("skills")),
            "experience": item.get("experience"), **_salary_from_text(item.get("salary")),
        }


ADAPTERS: dict[str, ApifyActorAdapter] = {a.key: a for a in (MultiBoardAdapter(), LinkedInAdapter(), IndeedAdapter(),
                                                             GlassdoorAdapter(), NaukriAdapter())}
PRIMARY = "multi_board"
# Sites with a dedicated fallback actor. ZipRecruiter and Bayt only run on the multi-board actor.
FALLBACK_FOR_SITE = {"linkedin": "linkedin", "indeed": "indeed", "glassdoor": "glassdoor", "naukri": "naukri"}


def get_adapter(key: str | None) -> ApifyActorAdapter:
    try:
        return ADAPTERS[key or ""]
    except KeyError:
        raise AdapterError(f"Unknown Apify adapter '{key}'") from None


def plan_runs(search: HunterSearch) -> tuple[list[PlannedRun], list[str]]:
    """Every actor run a search needs, with inputs built for each actor's own schema, plus warnings."""
    problems = search.problems()
    if problems:
        raise ValueError("; ".join(problems))
    groups: dict[str, list[str]] = {}
    for site in search.sites:
        key = FALLBACK_FOR_SITE.get(site, PRIMARY) if search.mode == "fallback" else PRIMARY
        groups.setdefault(key, []).append(site)
    runs, warnings = [], []
    for key, sites in groups.items():
        planned, notes = ADAPTERS[key].plan(search, sites)
        runs += planned
        warnings += notes
    if search.mode == "fallback" and PRIMARY in groups:
        warnings.append("ZipRecruiter and Bayt have no fallback actor, so they still use the multi-board actor.")
    return runs, list(dict.fromkeys(warnings))
