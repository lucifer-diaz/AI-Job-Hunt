"""Was this job what its search asked for? Deterministic, no model: job boards match keywords loosely.

    new job from a search run → assess(job, run's roles, location, primary skills)
      in scope      → carries on (domain filter already passed)
      out of scope  → hidden like the domain filter (Discovery → Not matching), never recovered, parsed or ranked;
                      the person can keep it (app.services.domain_match.keep)

Only a clear miss hides a job: a run that recorded no roles, a job without a location, or a description not fetched
yet is never held against it.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import Job, SearchRun
from app.services import domain_match
from app.services.apify_adapters import resolve_country
from app.services.domain_match import DESCRIPTION_WINDOW, Verdict, contains, role_tokens
from app.services.locations import location_key

ROLE, SKILL, LOCATION = "SCOPE_ROLE", "SCOPE_SKILL", "SCOPE_LOCATION"
# Cities that boards spell more than one way.
CITY_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai", "nyc": "new york",
                "new york city": "new york", "madras": "chennai", "calcutta": "kolkata", "new delhi": "delhi"}


def _city(location: str) -> str:
    key = re.sub(r"\s+", " ", location_key(location).lower()).strip()
    return CITY_ALIASES.get(key, key)


def _remote(location: str) -> bool:
    return "remote" in (location or "").lower()


def assess(job: Any, roles: list[str], location: str, skills: list[str], *, distance: int = 50) -> Verdict:
    """MATCH unless the title names none of the roles (or skills), the description none of the skills, or the
    location is in another country (or another city, with a zero search radius)."""
    title = getattr(job, "title", "") or ""
    words = role_tokens(roles)
    if words and not any(contains(title, w) for w in words) and not any(contains(title, s) for s in skills):
        return Verdict(False, ROLE, f"Not one of this search's roles: {', '.join(roles)}"[:300])

    description = (getattr(job, "description", "") or "")[:DESCRIPTION_WINDOW]
    if skills and description.strip() and not any(contains(f"{title}\n{description}", s) for s in skills):
        return Verdict(False, SKILL, f"Does not mention {', '.join(skills)}"[:300])

    where = getattr(job, "location", "") or ""
    if location.strip() and not _remote(location) and where.strip():
        wanted, found = resolve_country("", location), resolve_country("", where)
        if wanted and found and wanted.indeed_name != found.indeed_name:
            return Verdict(False, LOCATION, f"Outside this search's location: {where} (searched {location})"[:300])
        if not _remote(where) and distance == 0 and _city(where) != _city(location):
            return Verdict(False, LOCATION, f"Outside this search's location: {where} (searched {location})"[:300])
    return Verdict(True, domain_match.MATCH)


def screen(db: Session, job: Job, search_run_id: int | None, *, actor: str = "discovery") -> bool:
    """Hide a new job that its own search did not ask for. True when it may carry on."""
    if job.domain_match == domain_match.KEPT:
        return True
    run = db.get(SearchRun, search_run_id) if search_run_id else None
    if run is None or not run.roles:
        return True  # older runs and plain imports recorded no scope
    params = run.search_params or {}
    skills = [s for s in params.get("primary_skills") or [] if isinstance(s, str) and s.strip()]
    distance = params.get("distance")
    verdict = assess(job, list(run.roles), run.location or "", skills,
                     distance=50 if distance is None else int(distance))
    if verdict.match:
        return True
    domain_match.hide(db, job, verdict, actor=actor)
    return False
