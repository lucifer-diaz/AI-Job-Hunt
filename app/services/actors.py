"""Apify actor registry: turns one search profile into correct inputs for each selected actor.

Every built-in mapping follows the actor's published input schema. Actors disagree on field
names (searchTerm vs titleSearch vs keyword), on how many searches one run covers, and on which
filters exist, so each spec owns its input builder and its fan-out rule:

    role_location  one run per role × location
    location       one run per location, all roles in that run
    role           one run per role, all locations in that run
    once           a single run covering every role and location

Each spec may also own a `normalize` step that maps its dataset items onto the common job fields
(title, company, location, url, description, id, site) before app.services.sources imports them, for
actors whose items do not use the usual field names.

User-added actors describe their input with a JSON template using {{placeholders}}.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable

MAX_RUNS = 30
DEFAULT_ACTORS = ["openclawai/job-board-scraper"]
OPENCLAW_BOARDS = ["linkedin", "indeed", "glassdoor", "naukri", "bayt", "zip_recruiter"]
REMOTE_PLATFORMS = ["arbeitnow", "jobicy", "himalayas", "remoteok", "remotive", "workingnomads"]
SENIORITY_LEVELS = ["entry", "mid", "senior", "lead", "manager", "director", "executive"]
FAN_OUTS = ("role_location", "location", "role", "once")
# How the picker groups actors, in display order.
ACTOR_GROUPS = ("Job boards", "LinkedIn", "Remote boards", "Startups", "Your actors")
HN_MONTH_RE = re.compile(r"^(current|\d{4}-(0[1-9]|1[0-2]))$")
YC_BATCH_RE = re.compile(r"^[WSFX]\d{2}$", re.IGNORECASE)
PLACEHOLDERS = ("role", "roles", "location", "locations", "max_results", "hours_old", "days_old",
                "is_remote", "job_type", "sites", "country", "skill", "skills",
                # The rest of the setup form, so an actor can read any filter a person can set.
                "seniority", "distance", "offset", "easy_apply", "fetch_description", "enforce_annual_salary",
                "salary_only", "visa_sponsorship", "exclude_keywords", "linkedin_company_ids", "skip_agencies",
                "min_salary", "yc_batches", "hn_month", "startup_details", "remote_platforms", "remoteok_url")
REMOTE_WORDS = {"remote", "anywhere", "worldwide", "global"}


def is_remote_location(value: str) -> bool:
    return any(word in value.lower() for word in REMOTE_WORDS)


@dataclass
class ActorSpec:
    id: str
    name: str
    summary: str
    cost: str
    fan_out: str
    source: str                      # default board label when items carry no site field
    supports: list[str]
    build: Callable[[dict, list[str], list[str]], tuple[dict, list[str]]]
    location_hint: str = ""
    custom: bool = False
    extra: dict = field(default_factory=dict)
    group: str = "Job boards"
    # Maps one dataset item onto the common job fields; None when the item already uses them.
    normalize: Callable[[dict], dict] | None = None
    # The import stops at the setup's "Results per search": the builder sizes the run to bring in about that many.
    caps_total: bool = False

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "summary": self.summary, "cost": self.cost,
                "fan_out": self.fan_out, "supports": self.supports, "location_hint": self.location_hint,
                "custom": self.custom, "group": self.group, **self.extra}


def _days(hours: int | None) -> int:
    return max(1, math.ceil((hours or 0) / 24)) if hours else 0


def primary_skills(sp: dict) -> list[str]:
    """The setup's primary skills (Python, Java…), trimmed, at most three."""
    out: list[str] = []
    for skill in sp.get("primary_skills") or []:
        text = re.sub(r"\s+", " ", str(skill or "")).strip()[:40]
        if text and text.lower() not in {s.lower() for s in out}:
            out.append(text)
    return out[:3]


def with_skill(role: str, skills: list[str]) -> str:
    """The role's search term with the first primary skill added, unless the role already names it."""
    if not skills:
        return role
    from app.services.domain_match import contains

    return role if contains(role, skills[0]) else f"{role} {skills[0]}"


# ───────────── openclawai/job-board-scraper ─────────────
def build_openclaw(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    sites = [s for s in sp.get("sites") or [] if s in OPENCLAW_BOARDS] or ["linkedin", "indeed"]
    location = locations[0] if locations else ""
    terms = [with_skill(role, primary_skills(sp)) for role in roles[:5]]
    # The actor's maxResults counts per board and per search term; the setup's number is the run's total.
    total = int(sp.get("max_results") or 20)
    per_board = max(1, math.ceil(total / (len(sites) * len(terms))))
    if per_board * len(sites) * len(terms) > total:
        warnings.append(f"Multi Job Board: {total} results cannot be split evenly across {len(sites)} boards and "
                        f"{len(terms)} roles, so each asks for {per_board}; the import stops at {total}.")
    # Indeed and Glassdoor search one country: the run's location names it, else the setup's country.
    setting = (sp.get("country_indeed") or "usa").strip().lower()
    from app.services.search_scope import location_country

    found = location_country(location)
    country = found.indeed_name if found else setting
    if found and found.indeed_name != setting and {"indeed", "glassdoor"} & set(sites):
        warnings.append(f"Multi Job Board: Indeed and Glassdoor search {found.indeed_name} for '{location}', "
                        f"not the country setting ({setting}).")
    data: dict[str, Any] = {
        "searchTerm": terms[0],
        "location": location,
        "sites": sites,
        "maxResults": per_board,
        "isRemote": bool(sp.get("is_remote")),
        "distance": int(sp.get("distance") if sp.get("distance") is not None else 50),
        "countryIndeed": country,
        "easyApply": bool(sp.get("easy_apply")),
        "linkedinFetchDescription": bool(sp.get("fetch_description", True)),
        "descriptionFormat": "markdown",
        "enforceAnnualSalary": bool(sp.get("enforce_annual_salary")),
        "proxyConfiguration": {"useApifyProxy": True,
                               "apifyProxyGroups": ["RESIDENTIAL"] if sp.get("residential_proxy", True) else []},
    }
    if len(terms) > 1:
        data["searchTerms"] = terms
    if sp.get("job_type"):
        data["jobType"] = sp["job_type"]
    if sp.get("hours_old"):
        if data["easyApply"]:
            warnings.append("Multi Job Board: 'Posted within' is dropped because LinkedIn and Indeed cannot combine it with Easy Apply.")
        else:
            data["hoursOld"] = int(sp["hours_old"])
            if (sp.get("job_type") or data["isRemote"]) and "indeed" in sites:
                warnings.append("Multi Job Board: Indeed ignores job type and remote-only when 'Posted within' is set.")
    if sp.get("linkedin_company_ids"):
        data["linkedinCompanyIds"] = [str(x) for x in sp["linkedin_company_ids"]]
    if int(sp.get("offset") or 0) > 0:
        data["offset"] = int(sp["offset"])
    if data["sites"] != (sp.get("sites") or data["sites"]):
        warnings.append("Multi Job Board: only LinkedIn, Indeed, Glassdoor, Naukri, Bayt and ZipRecruiter are supported.")
    return data, warnings


# ───────────── fantastic-jobs/advanced-linkedin-job-search-api ─────────────
FANTASTIC_TYPES = {"fulltime": "FULL_TIME", "parttime": "PART_TIME", "contract": "CONTRACTOR",
                   "internship": "INTERN", "temporary": "TEMPORARY"}
FANTASTIC_SENIORITY = {"entry": "Entry level", "mid": "Mid-Senior level", "senior": "Mid-Senior level",
                       "lead": "Mid-Senior level", "manager": "Mid-Senior level", "director": "Director",
                       "executive": "Executive"}


def build_fantastic(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    hours = int(sp.get("hours_old") or 0)
    time_range = "1h" if 0 < hours <= 1 else "24h" if 0 < hours <= 24 else "7d" if 0 < hours <= 168 else "6m"
    places = [loc for loc in locations if not is_remote_location(loc)]
    per = int(sp.get("max_results") or 20)
    data: dict[str, Any] = {
        "titleSearch": roles,
        "timeRange": time_range,
        "limit": max(10, min(5000, per * len(roles) * max(1, len(places)))),
        "descriptionType": "text",
        "removeAgency": bool(sp.get("skip_agencies", True)),
    }
    if places:
        data["locationSearch"] = places
        if any(not re.search(r",.*,", p) for p in places):
            warnings.append("Advanced LinkedIn: locations must be 'City, State/Region, Country' (e.g. 'Bengaluru, Karnataka, India') or matches may be empty.")
    if sp.get("is_remote") or (locations and not places):
        data["aiWorkArrangementFilter"] = ["Remote OK", "Remote Solely"]
    if sp.get("job_type") in FANTASTIC_TYPES:
        data["aiEmploymentTypeFilter"] = [FANTASTIC_TYPES[sp["job_type"]]]
    levels = sorted({FANTASTIC_SENIORITY[s] for s in sp.get("seniority") or [] if s in FANTASTIC_SENIORITY})
    if levels:
        data["seniorityFilter"] = levels
    if sp.get("easy_apply"):
        data["directApply"] = True
    if sp.get("salary_only"):
        data["hasSalary"] = True
    if sp.get("visa_sponsorship"):
        data["aiVisaSponsorshipFilter"] = True
    if sp.get("exclude_keywords"):
        data["titleExclusionSearch"] = list(sp["exclude_keywords"])
    if time_range == "6m" and hours:
        warnings.append("Advanced LinkedIn: 'Posted within' longer than 7 days searches all active jobs (6 months).")
    return data, warnings


# ───────────── benthepythondev/remote-jobs-aggregator ─────────────
REMOTE_TYPES = {"fulltime": "full-time", "parttime": "part-time", "contract": "contract", "internship": "internship"}
REMOTE_SENIORITY = {"entry": "entry", "senior": "senior", "lead": "lead", "manager": "manager",
                    "director": "executive", "executive": "executive"}


def build_remote_aggregator(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    platforms = [p for p in sp.get("remote_platforms") or REMOTE_PLATFORMS if p in REMOTE_PLATFORMS] or REMOTE_PLATFORMS
    per = int(sp.get("max_results") or 20)
    data: dict[str, Any] = {
        "searchKeywords": roles[0],
        "platforms": platforms,
        "maxJobsPerPlatform": per,
        "maxTotalJobs": per * len(platforms),
        "postedWithinDays": _days(sp.get("hours_old")),
        "salaryOnly": bool(sp.get("salary_only")),
    }
    places = [loc for loc in locations if not is_remote_location(loc)]
    if places:
        data["locationKeywords"] = places
    if sp.get("job_type") in REMOTE_TYPES:
        data["jobTypes"] = [REMOTE_TYPES[sp["job_type"]]]
    levels = sorted({REMOTE_SENIORITY[s] for s in sp.get("seniority") or [] if s in REMOTE_SENIORITY})
    if levels:
        data["seniorityLevels"] = levels
    if sp.get("exclude_keywords"):
        data["excludeKeywords"] = list(sp["exclude_keywords"])
    return data, []


# ───────────── shahidirfan/Remoteok-Job-Scraper ─────────────
def build_remoteok(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    url = (sp.get("remoteok_url") or "https://remoteok.com/remote-jobs").strip()
    warnings = [] if url.startswith("https://remoteok.com") else ["RemoteOK: the start URL must be a remoteok.com page."]
    return {"startUrl": url if not warnings else "https://remoteok.com/remote-jobs", "keyword": roles[0],
            "results_wanted": int(sp.get("max_results") or 20), "proxyConfiguration": {"useApifyProxy": False}}, warnings


# ───────────── shared helpers for the startup boards ─────────────
def _places(locations: list[str]) -> list[str]:
    return [loc for loc in locations if not is_remote_location(loc)]


def _remote_only(sp: dict, locations: list[str]) -> bool:
    """Remote only when asked for, or when every location given is a remote one."""
    return bool(sp.get("is_remote")) or bool(locations and not _places(locations))


def _batches(sp: dict) -> list[str]:
    return [b.strip().upper() for b in sp.get("yc_batches") or [] if YC_BATCH_RE.match(b.strip())]


def _facts(pairs: list[tuple[str, Any]]) -> str:
    """Listing facts (salary, equity, visa) as text lines, so ranking sees them next to the description."""
    lines = []
    for label, value in pairs:
        if isinstance(value, list):
            value = ", ".join(str(v.get("name") if isinstance(v, dict) else v) for v in value if v)
        if value not in (None, "", [], {}) and not isinstance(value, bool):
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _join(description: Any, facts: str) -> str:
    text = str(description or "").strip()
    return "\n\n".join(part for part in (text, facts) if part)


def _where(location: Any, remote: Any) -> str:
    """The listing's location, marked remote when the listing says so (a remote role may still name a city)."""
    text = ", ".join(map(str, location)) if isinstance(location, list) else str(location or "").strip()
    if remote is not True or "remote" in text.lower():
        return text
    return f"Remote · {text}" if text else "Remote"


def _pick(item: dict, *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, "", [], {}):
            return item[key]
    return None


# ───────────── antishock/workatastartup-jobs-scraper ─────────────
# The actor filters by role family, not by title: each search role is mapped onto its family, and the
# ranker judges the titles that come back.
WAAS_FAMILIES = [
    (re.compile(r"design|\bux\b|\bui\b", re.IGNORECASE), "design"),
    (re.compile(r"sales|account exec|business develop|\bbdr\b|\bsdr\b", re.IGNORECASE), "sales"),
    (re.compile(r"market|growth|content|brand|seo", re.IGNORECASE), "marketing"),
    (re.compile(r"operat|\bops\b|finance|recruit|people|\bhr\b|chief of staff", re.IGNORECASE), "ops"),
    (re.compile(r"engineer|developer|scientist|machine learning|\bml\b|\bai\b|data|devops|\bsre\b|architect|"
                r"programmer|\bcto\b|technical", re.IGNORECASE), "eng"),
]


def waas_family(role: str) -> str | None:
    return next((family for pattern, family in WAAS_FAMILIES if pattern.search(role)), None)


def build_waas(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    families = list(dict.fromkeys(f for f in (waas_family(r) for r in roles) if f))
    unmatched = [r for r in roles if not waas_family(r)]
    if unmatched:
        warnings.append(f"Work at a Startup: no role family matches {', '.join(unmatched)}, so every role family is searched.")
        families = []
    if _places(locations):
        warnings.append("Work at a Startup cannot filter by city: listings from every location come back.")
    data: dict[str, Any] = {"roles": families, "remote": _remote_only(sp, locations),
                            "maxResults": int(sp.get("max_results") or 20),
                            "includeCompanyDetails": bool(sp.get("startup_details", True))}
    if _batches(sp):
        data["ycBatches"] = _batches(sp)
    return data, warnings


def normalize_waas(item: dict) -> dict:
    return {"id": _pick(item, "jobId", "id"), "site": "workatastartup",
            "title": _pick(item, "jobTitle", "title"), "company": _pick(item, "companyName", "company"),
            "location": _where(item.get("location"), item.get("remote")), "url": _pick(item, "jobUrl", "url", "companyUrl"),
            "description": _join(_pick(item, "description", "jobDescription", "descriptionText"), _facts([
                ("Salary", item.get("salary")), ("Equity", item.get("equity")),
                ("Experience", item.get("minExperience")), ("Job type", item.get("jobType")),
                ("Remote", item.get("remote") if isinstance(item.get("remote"), str) else None),
                ("Visa sponsorship", "yes" if item.get("sponsorsVisa") is True else None),
                ("YC batch", item.get("ycBatch")), ("Team size", item.get("teamSize")),
                ("Funding", item.get("funding")), ("Company", item.get("companyDescription")),
                ("Tags", item.get("tags"))])),
            "posted_at": item.get("postedAt")}


# ───────────── automation-lab/ycombinator-jobs-scraper ─────────────
YC_TYPES = {"fulltime": "Full-time", "internship": "Internship"}


def build_yc(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    per = int(sp.get("max_results") or 20)
    details = bool(sp.get("startup_details", True))
    data: dict[str, Any] = {"query": roles[0], "maxItems": per, "remoteOnly": _remote_only(sp, locations),
                            "includeIncomplete": False, "includeDetails": details}
    if details:
        data["maxDetailRequests"] = per
    if sp.get("job_type") in YC_TYPES:
        data["jobType"] = YC_TYPES[sp["job_type"]]
    elif sp.get("job_type"):
        warnings.append("Y Combinator Jobs: only full-time and internship job types can be filtered; any type is searched.")
    batches = _batches(sp)
    if batches:
        data["companyBatch"] = batches[0]
        if len(batches) > 1:
            warnings.append(f"Y Combinator Jobs: filters by one batch only, so {batches[0]} is used.")
    if _places(locations):
        warnings.append("Y Combinator Jobs cannot filter by city: listings from every location come back.")
    return data, warnings


def normalize_yc(item: dict) -> dict:
    return {"id": item.get("id"), "site": "ycombinator",
            "title": item.get("title"), "company": _pick(item, "companyName", "companySlug"),
            "location": _where(item.get("location"), item.get("isRemote")), "url": _pick(item, "url", "applyUrl"),
            "description": _join(_pick(item, "descriptionText", "descriptionHtml"), _facts([
                ("Salary", item.get("salaryRange")), ("Equity", item.get("equityRange")),
                ("Experience", item.get("minExperience")), ("Job type", item.get("type")),
                ("Visa", item.get("visa")), ("Skills", item.get("skills")),
                ("YC batch", item.get("companyBatch")), ("Company", item.get("companyOneLiner")),
                ("Industry", item.get("industry")), ("Team size", item.get("companySize"))])),
            "posted_at": item.get("createdAt")}


# ───────────── parseforge/hn-whoishiring-scraper ─────────────
def build_hn(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    month = str(sp.get("hn_month") or "current").strip().lower()
    if not HN_MONTH_RE.match(month):
        warnings.append("HN Who's Hiring: the thread month must be 'current' or YYYY-MM; the current thread is used.")
        month = "current"
    data: dict[str, Any] = {"keyword": roles[0], "month": month, "maxItems": int(sp.get("max_results") or 20),
                            "remoteOnly": _remote_only(sp, locations)}
    if int(sp.get("min_salary") or 0) > 0:
        data["minSalary"] = int(sp["min_salary"])
    if _places(locations):
        warnings.append("HN Who's Hiring cannot filter by city: postings from every location come back.")
    return data, warnings


def normalize_hn(item: dict) -> dict:
    return {"id": _pick(item, "hnCommentId", "commentId", "id"), "site": "hackernews",
            "title": _pick(item, "role", "title", "position"), "company": _pick(item, "company", "companyName"),
            "location": _where(item.get("location"), item.get("remote")), "url": _pick(item, "applyUrl", "url", "hnCommentUrl"),
            "description": _join(_pick(item, "description", "text", "descriptionText", "body", "fullText"), _facts([
                ("Salary", item.get("salaryRaw")), ("Job type", item.get("employmentType")),
                ("Stack", item.get("stackText")), ("Visa sponsorship", "yes" if item.get("visa") is True else None),
                ("Posted in", item.get("threadMonth") and f"HN Who's Hiring {item['threadMonth']}")])),
            "posted_at": _pick(item, "postedAt", "createdAt")}


# ───────────── blackfalcondata/wellfound-scraper ─────────────
WELLFOUND_TYPES = {"fulltime": "fulltime", "parttime": "parttime", "contract": "contract", "internship": "internship"}
# Wellfound filters by a minimum experience band, so the lowest seniority chosen sets it.
WELLFOUND_LEVELS = [("entry", "entry"), ("mid", "mid"), ("senior", "senior"), ("lead", "senior"),
                    ("manager", "senior"), ("director", "staff"), ("executive", "staff")]


def wellfound_slug(location: str) -> str:
    """'New York, NY' -> 'new-york': Wellfound's location slugs are the city alone."""
    return re.sub(r"[^a-z0-9]+", "-", location.split(",")[0].lower()).strip("-")


def build_wellfound(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    places = _places(locations)
    data: dict[str, Any] = {"query": roles[0], "maxResults": int(sp.get("max_results") or 20),
                            "remote": _remote_only(sp, locations), "descriptionFormat": "text"}
    if places:
        data["location"] = list(dict.fromkeys(wellfound_slug(p) for p in places))
    if sp.get("job_type") in WELLFOUND_TYPES:
        data["jobType"] = WELLFOUND_TYPES[sp["job_type"]]
    chosen = set(sp.get("seniority") or [])
    level = next((band for name, band in WELLFOUND_LEVELS if name in chosen), None)
    if level:
        data["experienceLevel"] = level
    if int(sp.get("min_salary") or 0) > 0:
        data["salaryMin"] = int(sp["min_salary"])
    if sp.get("visa_sponsorship"):
        data["visaSponsorship"] = True
    if sp.get("hours_old"):
        data["maxAgeMinutes"] = int(sp["hours_old"]) * 60
    if sp.get("exclude_keywords"):
        data["excludeKeywords"] = {"keywords": list(sp["exclude_keywords"]), "matchTitle": True,
                                   "matchDescription": False, "matchSkills": False}
    if sp.get("skip_agencies", True):
        data["skipReposts"] = True
    return data, []


def normalize_wellfound(item: dict) -> dict:
    return {"id": _pick(item, "id", "slug"), "site": "wellfound",
            "title": _pick(item, "title", "primaryRoleTitle"), "company": _pick(item, "companyName", "companySlug"),
            "location": _where(item.get("locationNames"), item.get("remote")), "url": _pick(item, "portalUrl", "detailUrl"),
            "description": _join(_pick(item, "description", "descriptionMarkdown", "descriptionHtml"), _facts([
                ("Compensation", item.get("compensation")), ("Job type", item.get("jobType")),
                ("Experience", item.get("yearsExperienceMin") and f"{item['yearsExperienceMin']}+ years"),
                ("Skills", item.get("skills")), ("Company", item.get("companyTagline")),
                ("Company size", item.get("companySizeLabel")), ("Stage", item.get("companyStage"))])),
            "posted_at": item.get("postedAt")}


# ───────────── crawlerbros/naukri-scraper ─────────────
# The Multi Job Board actor returns Naukri listings without their descriptions; this actor returns them with one.
NAUKRI_AGES = (1, 3, 7, 15, 30)
NAUKRI_TYPES = {"fulltime": "full-time", "parttime": "part-time", "internship": "internship", "contract": "contractual"}
# Naukri's own city slugs where they differ from the usual name.
NAUKRI_CITIES = {"bengaluru": "bangalore", "gurugram": "gurgaon", "delhi": "delhi-ncr", "new-delhi": "delhi-ncr",
                 "ncr": "delhi-ncr", "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata"}
NAUKRI_BASE = "https://www.naukri.com"
NAUKRI_ACTOR = "crawlerbros/naukri-scraper"


def naukri_city(location: str) -> str:
    """'Bengaluru, Karnataka, India' -> 'bangalore'."""
    slug = re.sub(r"[^a-z0-9]+", "-", location.split(",")[0].lower()).strip("-")
    return NAUKRI_CITIES.get(slug, slug)


def build_naukri(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    places = _places(locations)
    data: dict[str, Any] = {"keyword": roles[0], "maxItems": int(sp.get("max_results") or 20), "sortBy": "date"}
    if places:
        from app.services.apify_adapters import (
            resolve_country,  # local import: the Hunter adapters' country list
        )

        data["location"] = naukri_city(places[0])
        country = resolve_country("", places[0])
        if country is not None and country.iso != "IN":
            warnings.append(f"Naukri lists jobs in India: '{places[0]}' may return nothing.")
    if _remote_only(sp, locations):
        data["workMode"] = "remote"
    if sp.get("hours_old"):
        days = math.ceil(int(sp["hours_old"]) / 24)
        age = next((d for d in NAUKRI_AGES if days <= d), None)
        if age is None:
            warnings.append("Naukri: 'Posted within' is at most 30 days, so older postings are not searched.")
            age = NAUKRI_AGES[-1]
        data["jobAge"] = str(age)
    if sp.get("job_type") in NAUKRI_TYPES:
        data["jobType"] = NAUKRI_TYPES[sp["job_type"]]
    return data, warnings


def normalize_naukri(item: dict) -> dict:
    from app.services.job_description import (
        normalize_description,  # keeps paragraphs and bullets, inline tags flat
    )

    url = _pick(item, "url", "jdURL", "jobUrl") or ""
    description = _pick(item, "description", "jobDescription", "descriptionText")
    description = normalize_description(description) if isinstance(description, str) else description
    if isinstance(url, str) and url.startswith("/"):
        url = NAUKRI_BASE + url
    return {"id": _pick(item, "id", "jobId"), "site": "naukri",
            "title": _pick(item, "title", "jobTitle"), "company": _pick(item, "companyName", "company"),
            "location": _pick(item, "location", "placeholders"), "url": url,
            "description": _join(description, _facts([
                ("Experience", _pick(item, "experience", "experienceText")), ("Salary", item.get("salary")),
                ("Skills", _pick(item, "skills", "tagsAndSkills")), ("Work mode", item.get("workMode"))])),
            "posted_at": _pick(item, "postedDate", "datePosted", "createdDate", "footerPlaceholderLabel")}


BUILTIN: dict[str, ActorSpec] = {spec.id: spec for spec in [
    ActorSpec("openclawai/job-board-scraper", "Multi Job Board",
              "LinkedIn, Indeed, Glassdoor, Naukri, Bayt and ZipRecruiter in one run. Full descriptions come back for "
              "LinkedIn (with 'Fetch full LinkedIn descriptions') and Indeed; Naukri, ZipRecruiter and Bayt listings "
              "arrive without them.", "$0.005 per job",
              "location", "unknown",
              ["boards", "location", "distance", "country", "remote", "job_type", "hours", "easy_apply",
               "fetch_description", "annual_salary", "proxy", "linkedin_company_ids"], build_openclaw,
              caps_total=True),
    ActorSpec("crawlerbros/naukri-scraper", "Naukri",
              "Naukri.com jobs with their descriptions (the Multi Job Board actor returns Naukri without them). One "
              "run per role × city.", "Free actor · Apify usage only", "role_location", "naukri",
              ["location", "remote", "job_type", "hours"], build_naukri, normalize=normalize_naukri,
              location_hint="Naukri searches Indian cities: 'Bengaluru, India' searches bangalore; leave the "
                            "location empty to search all of India."),
    ActorSpec("fantastic-jobs/advanced-linkedin-job-search-api", "Advanced LinkedIn",
              "Indexed LinkedIn jobs with AI-enriched seniority, work arrangement, salary and visa data.",
              "$0.005 per job (less on paid tiers)", "once", "linkedin",
              ["location", "remote", "job_type", "hours", "easy_apply", "seniority", "salary_only",
               "visa_sponsorship", "exclude_keywords", "skip_agencies"], build_fantastic, group="LinkedIn",
              location_hint="Locations must be 'City, State/Region, Country', e.g. 'Bengaluru, Karnataka, India'."),
    ActorSpec("benthepythondev/remote-jobs-aggregator", "Remote Jobs Aggregator",
              "Arbeitnow, Jobicy, Himalayas, RemoteOK, Remotive and Working Nomads, deduplicated.",
              "$0.015 per job", "role", "remote",
              ["remote_platforms", "location", "job_type", "hours", "seniority", "salary_only", "exclude_keywords"],
              build_remote_aggregator, group="Remote boards", extra={"platforms": REMOTE_PLATFORMS}),
    ActorSpec("shahidirfan/Remoteok-Job-Scraper", "RemoteOK",
              "RemoteOK listings from the main feed or a category page.", "$0.001 per job + $0.005 per run",
              "role", "remoteok", ["remoteok_url"], build_remoteok, group="Remote boards"),
    ActorSpec("antishock/workatastartup-jobs-scraper", "Work at a Startup",
              "YC's own hiring board: roles at YC companies with salary, equity, batch and funding.",
              "$0.004 per job + $0.002 per run", "once", "workatastartup",
              ["remote", "yc_batches", "startup_details"], build_waas, group="Startups", normalize=normalize_waas,
              location_hint="Work at a Startup searches by role family (engineering, design, sales, marketing, ops) "
                            "and cannot filter by city."),
    ActorSpec("automation-lab/ycombinator-jobs-scraper", "Y Combinator Jobs",
              "The public ycombinator.com/jobs board, with salary, equity, visa and founders.",
              "$0.006 per run + under $0.0001 per job", "role", "ycombinator",
              ["remote", "job_type", "yc_batches", "startup_details"], build_yc, group="Startups",
              normalize=normalize_yc),
    ActorSpec("parseforge/hn-whoishiring-scraper", "HN Who's Hiring",
              "Every posting in a monthly Hacker News 'Who is hiring?' thread, with parsed salary.",
              "$0.02 per job", "role", "hackernews", ["remote", "hn_month", "min_salary"], build_hn,
              group="Startups", normalize=normalize_hn),
    ActorSpec("blackfalcondata/wellfound-scraper", "Wellfound",
              "Startup jobs from Wellfound (AngelList Talent) with salary and equity ranges.",
              "$0.003 per job + $0.005 per run", "role", "wellfound",
              ["location", "remote", "job_type", "hours", "seniority", "min_salary", "visa_sponsorship",
               "exclude_keywords", "skip_agencies"], build_wellfound, group="Startups",
              normalize=normalize_wellfound,
              location_hint="Wellfound matches the city name only: 'New York, NY' searches new-york."),
]}


# ───────────── user-added actors ─────────────
def _substitute(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: _substitute(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, values) for v in value]
    if isinstance(value, str):
        whole = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", value)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]

        def text(match: re.Match) -> str:
            item = values.get(match.group(1), match.group(0))
            return ", ".join(map(str, item)) if isinstance(item, list) else str(item)
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", text, value)
    return value


def unknown_placeholders(template: Any) -> list[str]:
    found = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", json.dumps(template)))
    return sorted(found - set(PLACEHOLDERS))


def custom_spec(entry: dict) -> ActorSpec:
    template = entry.get("input_template") or {}

    def build(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
        values = {
            "role": roles[0], "roles": roles, "location": locations[0] if locations else "", "locations": locations,
            "max_results": int(sp.get("max_results") or 20), "hours_old": int(sp.get("hours_old") or 0),
            "days_old": _days(sp.get("hours_old")), "is_remote": bool(sp.get("is_remote")),
            "job_type": sp.get("job_type") or "", "sites": sp.get("sites") or [],
            "country": sp.get("country_indeed") or "",
            "skill": (primary_skills(sp) or [""])[0], "skills": primary_skills(sp),
            "seniority": list(sp.get("seniority") or []), "distance": int(sp.get("distance") if sp.get("distance") is not None else 50),
            "offset": int(sp.get("offset") or 0), "easy_apply": bool(sp.get("easy_apply")),
            "fetch_description": bool(sp.get("fetch_description", True)),
            "enforce_annual_salary": bool(sp.get("enforce_annual_salary")), "salary_only": bool(sp.get("salary_only")),
            "visa_sponsorship": bool(sp.get("visa_sponsorship")),
            "exclude_keywords": list(sp.get("exclude_keywords") or []),
            "linkedin_company_ids": list(sp.get("linkedin_company_ids") or []),
            "skip_agencies": bool(sp.get("skip_agencies", True)), "min_salary": int(sp.get("min_salary") or 0),
            "yc_batches": list(sp.get("yc_batches") or []), "hn_month": sp.get("hn_month") or "current",
            "startup_details": bool(sp.get("startup_details", True)),
            "remote_platforms": list(sp.get("remote_platforms") or []), "remoteok_url": sp.get("remoteok_url") or "",
        }
        bad = unknown_placeholders(template)
        warnings = [f"{entry.get('name') or entry['id']}: unknown placeholders {', '.join(bad)} were sent as text."] if bad else []
        return _substitute(template, values), warnings

    return ActorSpec(entry["id"], entry.get("name") or entry["id"], "Added by you", "see the actor's pricing",
                     entry.get("fan_out") or "role_location", entry["id"].split("/")[-1], [], build, custom=True,
                     group="Your actors", extra={"input_template": template})


def catalog(sp: dict | None = None) -> dict[str, ActorSpec]:
    specs = dict(BUILTIN)
    for entry in (sp or {}).get("custom_actors") or []:
        if entry.get("id") and entry["id"] not in specs:
            specs[entry["id"]] = custom_spec(entry)
    return specs


def plan_runs(sp: dict) -> tuple[list[dict], list[str]]:
    """Expand a search profile into [{actor_id, input, location, roles}] plus warnings."""
    roles = [r.strip() for r in sp.get("roles") or [] if r.strip()]
    locations = [loc.strip() for loc in sp.get("locations") or [] if loc.strip()]
    if not roles:
        raise ValueError("Add at least one role to the search profile")
    # Only an absent selection means the defaults: an empty one (its last actor removed) runs nothing.
    selected = list(DEFAULT_ACTORS if sp.get("actors") is None else sp["actors"])
    specs = catalog(sp)
    missing = [a for a in selected if a not in specs]
    if missing:
        raise ValueError(f"Unknown actors: {', '.join(missing)}")
    if not selected:
        raise ValueError("Choose at least one Apify actor")
    runs, warnings = [], []
    # What each run was for, stored on its SearchRun: imported jobs are checked against it (app.services.search_scope).
    scope = {"primary_skills": primary_skills(sp), "is_remote": bool(sp.get("is_remote")),
             "distance": int(sp.get("distance") if sp.get("distance") is not None else 50)}
    for actor_id in selected:
        spec = specs[actor_id]
        if spec.fan_out == "role_location":
            groups = [([r], [loc] if loc else []) for r, loc in product(roles, locations or [""])]
        elif spec.fan_out == "location":
            groups = [(roles, [loc] if loc else []) for loc in (locations or [""])]
        elif spec.fan_out == "role":
            groups = [([r], locations) for r in roles]
        else:
            groups = [(roles, locations)]
        for group_roles, group_locations in groups:
            data, notes = spec.build(sp, group_roles, group_locations)
            warnings.extend(notes)
            params = {**scope, "locations": group_locations,
                      **({"max_jobs": int(sp.get("max_results") or 20)} if spec.caps_total else {})}
            runs.append({"actor_id": actor_id, "actor_name": spec.name, "input": data,
                         "location": ", ".join(group_locations), "roles": group_roles, "search_params": params})
    if len(runs) > MAX_RUNS:
        raise ValueError(f"This profile would start {len(runs)} runs; the limit is {MAX_RUNS}. Remove roles, locations or actors.")
    boards = set(sp.get("sites") or [])
    if "openclawai/job-board-scraper" in selected and "naukri" in boards:
        warnings.append("Multi Job Board returns Naukri listings without descriptions"
                        + (": untick Naukri there — the Naukri actor already searches it, with descriptions."
                           if NAUKRI_ACTOR in selected else ". Add the Naukri actor for Naukri jobs with descriptions."))
    return runs, list(dict.fromkeys(warnings))


def default_site_for(actor_id: str, sp: dict | None = None) -> str:
    spec = catalog(sp).get(actor_id)
    return spec.source if spec else actor_id.split("/")[-1].lower()


def item_normalizer(actor_id: str) -> Callable[[dict], dict] | None:
    """The actor's own dataset-item mapping, when it has one. Only built-in actors do."""
    spec = BUILTIN.get(actor_id)
    return spec.normalize if spec else None


LABELS = {
    "searchTerm": "Role", "searchTerms": "Roles", "titleSearch": "Roles", "searchKeywords": "Keywords", "keyword": "Keyword",
    "location": "Location", "locationSearch": "Locations", "locationKeywords": "Locations",
    "sites": "Boards", "platforms": "Boards", "startUrl": "Start URL",
    "maxResults": "Per board", "limit": "Limit", "maxJobsPerPlatform": "Per board", "results_wanted": "Limit",
    "hoursOld": "Posted ≤ hours", "timeRange": "Time range", "postedWithinDays": "Posted ≤ days",
    "jobType": "Job type", "aiEmploymentTypeFilter": "Job type", "jobTypes": "Job type",
    "isRemote": "Remote only", "aiWorkArrangementFilter": "Work arrangement", "easyApply": "Easy Apply only",
    "directApply": "Easy Apply only", "countryIndeed": "Indeed country", "distance": "Radius (mi)",
    "seniorityFilter": "Seniority", "seniorityLevels": "Seniority", "removeAgency": "Skip agencies",
    "hasSalary": "Salary listed", "salaryOnly": "Salary listed", "linkedinFetchDescription": "Full LinkedIn descriptions",
    "enforceAnnualSalary": "Annual salary", "titleExclusionSearch": "Exclude", "excludeKeywords": "Exclude",
    "roles": "Role families", "remote": "Remote only", "ycBatches": "YC batches", "companyBatch": "YC batch",
    "includeCompanyDetails": "Company details", "includeDetails": "Full descriptions", "query": "Keywords",
    "maxItems": "Limit", "remoteOnly": "Remote only", "month": "Thread month", "minSalary": "Min salary",
    "salaryMin": "Min salary", "experienceLevel": "Experience", "visaSponsorship": "Visa sponsorship",
    "maxAgeMinutes": "Posted ≤ minutes", "skipReposts": "Skip reposts", "maxDetailRequests": "Detail pages",
}


def describe_input(data: dict) -> list[list[str]]:
    rows = []
    for key, value in (data or {}).items():
        if key in ("proxyConfiguration", "descriptionFormat", "descriptionType", "includeIncomplete") \
                or value in (None, "", [], {}, False, 0):
            continue
        if isinstance(value, dict) and "keywords" in value:
            value = value["keywords"]
        shown = " · ".join(map(str, value)) if isinstance(value, list) else "yes" if value is True else str(value)
        rows.append([LABELS.get(key, key), shown[:300]])
    return rows
