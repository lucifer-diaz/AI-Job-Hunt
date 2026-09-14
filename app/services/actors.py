"""Apify actor registry: turns one search profile into correct inputs for each selected actor.

Every built-in mapping follows the actor's published input schema. Actors disagree on field
names (searchTerm vs titleSearch vs keyword), on how many searches one run covers, and on which
filters exist, so each spec owns its input builder and its fan-out rule:

    role_location  one run per role × location
    location       one run per location, all roles in that run
    role           one run per role, all locations in that run
    once           a single run covering every role and location

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
PLACEHOLDERS = ("role", "roles", "location", "locations", "max_results", "hours_old", "days_old",
                "is_remote", "job_type", "sites", "country")
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

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "summary": self.summary, "cost": self.cost,
                "fan_out": self.fan_out, "supports": self.supports, "location_hint": self.location_hint,
                "custom": self.custom, **self.extra}


def _days(hours: int | None) -> int:
    return max(1, math.ceil((hours or 0) / 24)) if hours else 0


# ───────────── openclawai/job-board-scraper ─────────────
def build_openclaw(sp: dict, roles: list[str], locations: list[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    sites = [s for s in sp.get("sites") or [] if s in OPENCLAW_BOARDS] or ["linkedin", "indeed"]
    data: dict[str, Any] = {
        "searchTerm": roles[0],
        "location": locations[0] if locations else "",
        "sites": sites,
        "maxResults": int(sp.get("max_results") or 20),
        "isRemote": bool(sp.get("is_remote")),
        "distance": int(sp.get("distance") if sp.get("distance") is not None else 50),
        "countryIndeed": (sp.get("country_indeed") or "usa").strip().lower(),
        "easyApply": bool(sp.get("easy_apply")),
        "linkedinFetchDescription": bool(sp.get("fetch_description", True)),
        "descriptionFormat": "markdown",
        "enforceAnnualSalary": bool(sp.get("enforce_annual_salary")),
        "proxyConfiguration": {"useApifyProxy": True,
                               "apifyProxyGroups": ["RESIDENTIAL"] if sp.get("residential_proxy", True) else []},
    }
    if len(roles) > 1:
        data["searchTerms"] = roles[:5]
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


BUILTIN: dict[str, ActorSpec] = {spec.id: spec for spec in [
    ActorSpec("openclawai/job-board-scraper", "Multi Job Board",
              "LinkedIn, Indeed, Glassdoor, Naukri, Bayt and ZipRecruiter in one run.", "$0.005 per job",
              "location", "unknown",
              ["boards", "location", "distance", "country", "remote", "job_type", "hours", "easy_apply",
               "fetch_description", "annual_salary", "proxy", "linkedin_company_ids"], build_openclaw),
    ActorSpec("fantastic-jobs/advanced-linkedin-job-search-api", "Advanced LinkedIn",
              "Indexed LinkedIn jobs with AI-enriched seniority, work arrangement, salary and visa data.",
              "$0.005 per job (less on paid tiers)", "once", "linkedin",
              ["location", "remote", "job_type", "hours", "easy_apply", "seniority", "salary_only",
               "visa_sponsorship", "exclude_keywords", "skip_agencies"], build_fantastic,
              location_hint="Locations must be 'City, State/Region, Country', e.g. 'Bengaluru, Karnataka, India'."),
    ActorSpec("benthepythondev/remote-jobs-aggregator", "Remote Jobs Aggregator",
              "Arbeitnow, Jobicy, Himalayas, RemoteOK, Remotive and Working Nomads, deduplicated.",
              "$0.015 per job", "role", "remote",
              ["remote_platforms", "location", "job_type", "hours", "seniority", "salary_only", "exclude_keywords"],
              build_remote_aggregator, extra={"platforms": REMOTE_PLATFORMS}),
    ActorSpec("shahidirfan/Remoteok-Job-Scraper", "RemoteOK",
              "RemoteOK listings from the main feed or a category page.", "$0.001 per job + $0.005 per run",
              "role", "remoteok", ["remoteok_url"], build_remoteok),
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
        }
        bad = unknown_placeholders(template)
        warnings = [f"{entry.get('name') or entry['id']}: unknown placeholders {', '.join(bad)} were sent as text."] if bad else []
        return _substitute(template, values), warnings

    return ActorSpec(entry["id"], entry.get("name") or entry["id"], "Added by you", "see the actor's pricing",
                     entry.get("fan_out") or "role_location", entry["id"].split("/")[-1], [], build, custom=True,
                     extra={"input_template": template})


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
    selected = [a for a in sp.get("actors") or DEFAULT_ACTORS]
    specs = catalog(sp)
    missing = [a for a in selected if a not in specs]
    if missing:
        raise ValueError(f"Unknown actors: {', '.join(missing)}")
    if not selected:
        raise ValueError("Choose at least one Apify actor")
    runs, warnings = [], []
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
            runs.append({"actor_id": actor_id, "actor_name": spec.name, "input": data,
                         "location": ", ".join(group_locations), "roles": group_roles})
    if len(runs) > MAX_RUNS:
        raise ValueError(f"This profile would start {len(runs)} runs; the limit is {MAX_RUNS}. Remove roles, locations or actors.")
    return runs, list(dict.fromkeys(warnings))


def default_site_for(actor_id: str, sp: dict | None = None) -> str:
    spec = catalog(sp).get(actor_id)
    return spec.source if spec else actor_id.split("/")[-1].lower()


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
}


def describe_input(data: dict) -> list[list[str]]:
    rows = []
    for key, value in (data or {}).items():
        if key in ("proxyConfiguration", "descriptionFormat", "descriptionType") or value in (None, "", [], {}, False, 0):
            continue
        shown = " · ".join(map(str, value)) if isinstance(value, list) else "yes" if value is True else str(value)
        rows.append([LABELS.get(key, key), shown[:300]])
    return rows
