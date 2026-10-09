from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, SearchRun
from app.services import system_log, tracing
from app.services.apify_adapters import parse_posted_date
from app.services.external_api import external_client
from app.services.user_settings import apify_token
from app.settings import get_settings
from app.tenancy import require_user_id

MISSING_APIFY_TOKEN = "Add your Apify API token in Settings to run searches"


UA = "JobHuntOS/0.2 (+personal-job-search-tool)"
APIFY_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def _source_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]


def upsert_job(
    db: Session,
    *,
    source: str,
    source_ref: str,
    title: str,
    company: str,
    location: str = "",
    url: str = "",
    description: str = "",
    raw: dict | None = None,
    user_id: int | None = None,
    posted_at: datetime | None = None,
    posted_date_raw: str | None = None,
    search_run_id: int | None = None,
) -> tuple[Job, bool]:
    from app.services import (  # local import: sources is imported while the app is starting up
        discovery,
        jd_recovery,
        job_description,
    )

    owner = user_id if user_id is not None else require_user_id(db)
    existing = db.scalar(select(Job).where(Job.user_id == owner, Job.source == source, Job.source_ref == source_ref))
    if existing:
        if description and len(description) > len(existing.description or ""):
            existing.description = description
            # A fuller posting is new employer text: parse it again (app.services.job_description).
            job_description.queue_parse(db, existing, reason="description_changed")
            # A job held back for having no description can be ranked now that it has one.
            from app.services.ranker import reopen_for_description

            reopen_for_description(db, existing)
            # Still a stub? Recovery may read the posting; a real description cancels recovery not yet started.
            jd_recovery.on_description_changed(db, existing)
        if url:
            existing.url = url
        if raw:
            existing.raw = raw
        if existing.posted_at is None and posted_at is not None:
            existing.posted_at, existing.posted_date_raw = posted_at, posted_date_raw
        db.commit()
        return existing, False
    job = Job(
        user_id=owner,
        source=source,
        source_ref=source_ref,
        title=(title or "Untitled")[:300],
        company=(company or "Unknown company")[:300],
        location=(location or "")[:300],
        url=url or "",
        description=description or "",
        raw=raw or {},
        posted_at=posted_at,
        posted_date_raw=posted_date_raw,
        search_run_id=search_run_id,
    )
    db.add(job)
    db.flush()
    # Resume choice, domain filter, then parse or (for an unusable description) background recovery. Only rows are
    # written here, so ingestion never waits for any of it (app.services.discovery).
    discovery.admit(db, job, search_run_id=search_run_id)
    db.commit()
    db.refresh(job)
    return job, True


def _fetch_lever(site: str) -> list[dict]:
    with external_client("JOB_BOARD", service="lever", operation="board_fetch", timeout=30,
                         metadata={"board": site}) as client:
        resp = client.get(f"https://api.lever.co/v0/postings/{site}?mode=json")
        resp.raise_for_status()
        return resp.json()


def _fetch_greenhouse(board_token: str) -> list[dict]:
    with external_client("JOB_BOARD", service="greenhouse", operation="board_fetch", timeout=30,
                         metadata={"board": board_token}) as client:
        resp = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true")
        resp.raise_for_status()
        return resp.json().get("jobs", [])


def _lever_teams(item: dict) -> list[str]:
    categories = item.get("categories") or {}
    return [t for t in (categories.get("team"), categories.get("department")) if t]


def _greenhouse_teams(item: dict) -> list[str]:
    return [d.get("name") for d in item.get("departments") or [] if d.get("name")]


def _board_filter(items: list[dict], teams_of, title_of, teams: list[str] | None, roles: list[str] | None) -> list[dict]:
    """Keep the postings in one of the chosen teams (name equals or contains it) whose title has a role keyword.

    No teams and no roles keeps everything, exactly as before. Both set means both must match.
    """
    wanted_teams = [t.strip().lower() for t in teams or [] if t.strip()]
    wanted_roles = [r.strip().lower() for r in roles or [] if r.strip()]
    kept = []
    for item in items:
        if wanted_teams and not any(w in t.lower() for t in teams_of(item) for w in wanted_teams):
            continue
        if wanted_roles and not any(w in (title_of(item) or "").lower() for w in wanted_roles):
            continue
        kept.append(item)
    return kept


def board_teams(kind: str, token: str) -> list[dict[str, Any]]:
    """The teams (departments) a company's board has open roles in, with counts, so a person can pick some."""
    fetch, teams_of = (_fetch_lever, _lever_teams) if kind == "lever" else (_fetch_greenhouse, _greenhouse_teams)
    counts: dict[str, int] = {}
    items = fetch(token)
    for item in items:
        for team in dict.fromkeys(teams_of(item)):
            counts[team] = counts.get(team, 0) + 1
    teams = [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return teams


def import_lever(db: Session, site: str, teams: list[str] | None = None, roles: list[str] | None = None) -> dict[str, int]:
    everything = _fetch_lever(site)
    data = _board_filter(everything, _lever_teams, lambda i: i.get("text"), teams, roles)
    created = 0
    for item in data:
        categories = item.get("categories") or {}
        lists = item.get("lists") or []
        list_text = "\n".join(
            f"{x.get('text','')}: {BeautifulSoup(x.get('content',''), 'html.parser').get_text(' ', strip=True)}"
            for x in lists
        )
        description = "\n\n".join(
            filter(
                None,
                [
                    item.get("descriptionPlain")
                    or BeautifulSoup(item.get("description", ""), "html.parser").get_text(" ", strip=True),
                    list_text,
                    item.get("additionalPlain")
                    or BeautifulSoup(item.get("additional", ""), "html.parser").get_text(" ", strip=True),
                ],
            )
        )
        _, is_new = upsert_job(
            db,
            source="lever",
            source_ref=str(item.get("id")),
            title=item.get("text") or "Untitled",
            company=site,
            location=categories.get("location") or "",
            url=item.get("hostedUrl") or item.get("applyUrl") or "",
            description=description,
            raw=item,
        )
        created += int(is_new)
    return {"seen": len(data), "created": created, "total": len(everything)}


def import_greenhouse(db: Session, board_token: str, teams: list[str] | None = None,
                      roles: list[str] | None = None) -> dict[str, int]:
    everything = _fetch_greenhouse(board_token)
    data = _board_filter(everything, _greenhouse_teams, lambda i: i.get("title"), teams, roles)
    created = 0
    for item in data:
        description = BeautifulSoup(item.get("content", ""), "html.parser").get_text("\n", strip=True)
        _, is_new = upsert_job(
            db,
            source="greenhouse",
            source_ref=str(item.get("id")),
            title=item.get("title") or "Untitled",
            company=board_token,
            location=(item.get("location") or {}).get("name", ""),
            url=item.get("absolute_url") or "",
            description=description,
            raw=item,
        )
        created += int(is_new)
    return {"seen": len(data), "created": created, "total": len(everything)}


def fetch_job_page(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    with external_client("WEB", service="job_posting", operation="page_fetch", timeout=25,
                         follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return text[:50000]


def _apify_actor_api_id(actor_id: str) -> str:
    """Apify accepts Store actor names in username~actor-name form in REST URLs."""
    return actor_id.strip().replace("/", "~")


def _first(item: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(_first(value, "name", "formatted", "text", "title", default=""))
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value if v)
    return str(value or "")


def normalize_apify_job(item: dict[str, Any], default_site: str = "unknown") -> dict[str, Any] | None:
    """Normalize job items from the built-in actors and common third-party field names."""
    title = _text(_first(item, "title", "job_title", "jobTitle", "position", "name")).strip()
    company = _text(_first(item, "company", "company_name", "companyName", "organization", "employer",
                           "employer_name", "hiringOrganization")).strip()
    if not title or not company:
        return None

    location = _text(_first(item, "location", "job_location", "jobLocation", "formattedLocation",
                            "locations_derived", "locations_raw", "candidate_required_location", "locations"))
    url = _text(_first(item, "job_url_direct", "apply_url", "applyUrl", "applicationUrl", "application_url",
                       "job_url", "jobUrl", "url", "link", "listing_url"))
    description = _first(item, "description", "description_text", "job_description", "jobDescription",
                         "descriptionText", "description_html", "descriptionHtml", "text", "content")
    if isinstance(description, str) and "<" in description and ">" in description:
        description = BeautifulSoup(description, "html.parser").get_text("\n", strip=True)
    description = str(description or "")[:100000]

    site = _text(_first(item, "site", "source", "platform", "board", default=default_site)).lower().replace(" ", "_")
    raw_id = _first(item, "id", "job_id", "jobId", "jobKey", "external_id", "slug", "fingerprint")
    if raw_id:
        source_ref = f"{site}:{raw_id}"
    elif url:
        source_ref = f"{site}:{_source_ref(url)}"
    else:
        source_ref = f"{site}:{_source_ref(f'{company}|{title}|{location}') }"

    # Only actor item mappings (app.services.actors) supply `posted_at`; the date is parsed, never guessed.
    posted = parse_posted_date(item.get("posted_at"), None)
    return {
        "posted_at": posted.at,
        "posted_date_raw": posted.raw,
        "source": f"apify:{site}",
        "source_ref": source_ref,
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "description": description,
        "raw": item,
    }


def dedupe_key(title: str, company: str, location: str = "") -> str:
    """Same company + title + city is one job, whichever actor or board it came from."""
    city = (location or "").split(",")[0]
    return re.sub(r"[^a-z0-9]+", " ", f"{company}|{title}|{city}".lower()).strip()


def import_apify_items(db: Session, items: list[dict[str, Any]], search_run_id: int | None = None,
                       default_site: str = "unknown", user_id: int | None = None,
                       adapt: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, int]:
    """Import dataset items. `adapt` is the actor's own item mapping (app.services.actors.item_normalizer);
    the job keeps the item exactly as the actor returned it in `raw`."""
    owner = user_id if user_id is not None else require_user_id(db)
    from app.services import job_removal

    created = normalized = skipped = duplicates = not_matching = previously_removed = over_limit = 0
    run = db.get(SearchRun, search_run_id) if search_run_id else None
    # A setup's "Results per search" is the most jobs one run brings in (actors.ActorSpec.caps_total).
    max_jobs = ((run.search_params or {}).get("max_jobs") if run is not None else None) or None
    # A pulled-again run counts the jobs it already brought in, so repeated pulls never pass the limit.
    imported = (db.scalar(select(func.count(Job.id)).where(Job.user_id == owner, Job.search_run_id == run.id)) or 0
                if max_jobs else 0)
    known = {dedupe_key(t, c, loc) for t, c, loc in
             db.execute(select(Job.title, Job.company, Job.location).where(Job.user_id == owner)).all()}
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if max_jobs and imported + created >= max_jobs:
            over_limit += 1
            continue
        job_data = normalize_apify_job(adapt(item) if adapt else item, default_site)
        if not job_data:
            skipped += 1
            continue
        job_data["raw"] = item
        normalized += 1
        key = dedupe_key(job_data["title"], job_data["company"], job_data["location"])
        same_listing = db.scalar(select(Job.id).where(Job.user_id == owner, Job.source == job_data["source"],
                                                      Job.source_ref == job_data["source_ref"]))
        if not same_listing and key in known:
            duplicates += 1  # already in the pipeline from another actor or board
            continue
        if not same_listing and job_removal.is_removed(db, owner, source=job_data["source"],
                                                       source_ref=job_data["source_ref"], dedupe_key=key):
            previously_removed += 1  # deleted earlier because the posting no longer existed
            continue
        job, is_new = upsert_job(db, **job_data, user_id=owner, search_run_id=search_run_id)
        if not is_new:
            duplicates += 1
            continue
        known.add(key)
        created += 1
        not_matching += int(job.domain_match == "NOT_MATCHING")
    return {"seen": len(items), "normalized": normalized, "created": created, "skipped": skipped,
            "duplicates": duplicates, "not_matching": not_matching, "previously_removed": previously_removed,
            "over_limit": over_limit}


def build_apify_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Build default Actor input, or pass raw_actor_input verbatim for any other Actor schema."""
    raw_actor_input = payload.get("raw_actor_input") or {}
    if raw_actor_input:
        return dict(raw_actor_input)

    terms = [str(x).strip() for x in payload.get("search_terms", []) if str(x).strip()]
    if not terms:
        raise ValueError("At least one search term is required")

    # openclawai/job-board-scraper reads searchTerm; searchTerms is only used for multi-term runs.
    actor_input: dict[str, Any] = {
        "searchTerm": terms[0],
        "maxResults": int(payload.get("max_results", 20)),
        "descriptionFormat": "markdown",
    }
    if len(terms) > 1:
        actor_input["searchTerms"] = terms[:5]
    if payload.get("easy_apply"):
        payload = {**payload, "hours_old": None}  # the actor cannot combine hoursOld with easyApply
    optional_map = {
        "location": "location",
        "sites": "sites",
        "country_indeed": "countryIndeed",
        "hours_old": "hoursOld",
        "job_type": "jobType",
        "is_remote": "isRemote",
        "easy_apply": "easyApply",
        "linkedin_fetch_description": "linkedinFetchDescription",
        "distance": "distance",
        "enforce_annual_salary": "enforceAnnualSalary",
    }
    for source_key, actor_key in optional_map.items():
        value = payload.get(source_key)
        if isinstance(value, bool):
            if value:
                actor_input[actor_key] = value
        elif value not in (None, "", []):
            actor_input[actor_key] = value

    # This makes the integration adaptable to another Apify Actor without code changes.
    actor_input.update(payload.get("custom_input") or {})
    return actor_input


APIFY_API = "https://api.apify.com/v2"
# Every Apify read names the operation it is performing, so Monitoring can tell a dataset page fetch from a
# status poll. Anything not named here is recorded under the path's first segment.
APIFY_OPERATIONS = {"actor-runs": "actor_run_status", "datasets": "dataset_fetch", "acts": "actor_lookup",
                    "key-value-stores": "key_value_fetch"}


def _apify_operation(path: str) -> str:
    return APIFY_OPERATIONS.get(path.strip("/").split("/")[0], "api_request")


def apify_client(token: str, *, operation: str, user_id: int | None = None, metadata: dict[str, Any] | None = None,
                 timeout: float | int = 30, json_body: bool = False, service: str = "apify_api",
                 job_id: int | None = None, workflow_type: str | None = None):
    """The instrumented HTTP client every Apify call uses. Telemetry comes with it; nothing else is needed."""
    headers = {"Authorization": f"Bearer {token}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return external_client("APIFY", service=service, operation=operation, timeout=timeout, headers=headers,
                           user_id=user_id, metadata=metadata or {}, job_id=job_id, workflow_type=workflow_type)


def apify_get(path: str, params: dict[str, Any] | None = None, token: str | None = None, *,
              operation: str | None = None, metadata: dict[str, Any] | None = None) -> Any:
    """GET an Apify API path with a user's token. All Apify reads go through here."""
    if not token:
        raise RuntimeError(MISSING_APIFY_TOKEN)
    with apify_client(token, operation=operation or _apify_operation(path), metadata=metadata) as client:
        resp = client.get(f"{APIFY_API}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


def apify_post(path: str, payload: dict[str, Any], token: str | None = None, *, operation: str | None = None,
               metadata: dict[str, Any] | None = None) -> Any:
    """POST to an Apify API path with a user's token. Starting actor runs goes through here."""
    if not token:
        raise RuntimeError(MISSING_APIFY_TOKEN)
    with apify_client(token, operation=operation or "actor_run_start", metadata=metadata, json_body=True,
                      timeout=get_settings().apify_request_timeout_seconds) as client:
        resp = client.post(f"{APIFY_API}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()


def start_apify_search(db: Session, payload: dict[str, Any]) -> SearchRun:
    """Start an Actor run with the signed-in user's own Apify token."""
    settings = get_settings()
    owner = require_user_id(db)
    token = apify_token(db, owner)
    if not token:
        raise RuntimeError(MISSING_APIFY_TOKEN)

    actor_id = (payload.get("actor_id") or settings.apify_actor_id).strip()
    # Search profiles pass an input already built for this actor's schema.
    actor_input = payload.get("actor_input") or build_apify_input(payload)
    api_actor_id = _apify_actor_api_id(actor_id)
    with apify_client(token, operation="actor_run_start", user_id=owner, json_body=True,
                      metadata={"actor_id": actor_id, "location": payload.get("location") or None}) as client:
        resp = client.post(f"{APIFY_API}/actors/{api_actor_id}/runs", json=actor_input)
        resp.raise_for_status()
        run = (resp.json() or {}).get("data") or {}

    external_id = str(run.get("id") or "")
    if not external_id:
        raise RuntimeError("Apify did not return a run ID")

    search_run = SearchRun(
        user_id=owner,
        provider="apify",
        external_run_id=external_id,
        actor_id=actor_id,
        input_json=actor_input,
        # The run's scope: imported jobs outside these roles or this location are hidden (app.services.search_scope).
        roles=list(payload.get("roles") or []) or None,
        location=payload.get("location") or None,
        search_params=payload.get("search_params") or None,
        status=str(run.get("status") or "READY"),
        dataset_id=str(run.get("defaultDatasetId") or ""),
    )
    db.add(search_run)
    db.commit()
    db.refresh(search_run)
    return search_run


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _number(value: Any) -> float | None:
    """A number Apify reported, or None. Never a guess: an unparsable value is simply unknown."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apify_usage(run: dict[str, Any]) -> dict[str, Any]:
    """The usage Apify reports for a run. Anything Apify did not return stays None — nothing is estimated."""
    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    options = run.get("options") if isinstance(run.get("options"), dict) else {}
    stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
    compute = _number(usage.get("ACTOR_COMPUTE_UNITS"))
    if compute is None:
        compute = _number(stats.get("computeUnits"))
    return {"compute_units": compute,
            "usage_total_usd": _number(run.get("usageTotalUsd")),
            "memory_mb": _number(options.get("memoryMbytes")),
            "usage_breakdown": {k: v for k, v in usage.items() if _number(v) is not None} or None}


def apply_apify_usage(search_run: SearchRun, run: dict[str, Any]) -> None:
    """Copy Apify's own usage figures onto the run, leaving anything it did not report untouched."""
    reported = apify_usage(run)
    if reported["compute_units"] is not None:
        search_run.compute_units = reported["compute_units"]
    if reported["usage_total_usd"] is not None:
        search_run.usage_total_usd = Decimal(str(reported["usage_total_usd"]))
    if reported["memory_mb"] is not None:
        search_run.memory_mb = int(reported["memory_mb"])
    stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
    if reported["usage_breakdown"] or stats:
        search_run.stats = {**(search_run.stats or {}),
                            "apify_usage": reported["usage_breakdown"],
                            "apify_run_stats": {k: v for k, v in stats.items() if isinstance(v, (int, float))} or None}


def _apify_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None


def _actor_name(act_id: str, cache: dict[str, tuple[str, str]], token: str) -> tuple[str, str]:
    """Return (username/name, title) for an actor ID, falling back to the raw ID."""
    if act_id not in cache:
        try:
            info = (apify_get(f"/acts/{act_id}", token=token, operation="actor_lookup",
                              metadata={"actor_id": act_id}) or {}).get("data") or {}
            full = f"{info['username']}/{info['name']}" if info.get("username") and info.get("name") else act_id
            cache[act_id] = (full, info.get("title") or info.get("name") or full)
        except (httpx.HTTPError, TypeError, ValueError):
            cache[act_id] = (act_id, act_id)
    return cache[act_id]


def list_remote_runs(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    """Recent runs in the user's Apify account, including ones started outside Job Hunt OS."""
    token = apify_token(db)
    items = ((apify_get("/actor-runs", {"desc": 1, "limit": limit}, token=token,
                        operation="actor_run_list") or {}).get("data") or {}).get("items") or []
    ids = [str(item.get("id")) for item in items if item.get("id")]
    local = {r.external_run_id: r for r in db.scalars(select(SearchRun).where(SearchRun.external_run_id.in_(ids))).all()} if ids else {}
    names: dict[str, tuple[str, str]] = {}
    out = []
    for item in items:
        run_id = str(item.get("id") or "")
        full, title = _actor_name(str(item.get("actId") or ""), names, token)
        count = None
        if item.get("defaultDatasetId") and item.get("status") in APIFY_TERMINAL_STATES:
            try:
                count = ((apify_get(f"/datasets/{item['defaultDatasetId']}", token=token,
                                    operation="dataset_info",
                                    metadata={"dataset_id": item["defaultDatasetId"], "run_id": run_id})
                          or {}).get("data") or {}).get("itemCount")
            except httpx.HTTPError:
                count = None
        tracked = local.get(run_id)
        out.append({
            "external_run_id": run_id, "actor_id": full, "actor_title": title, "status": str(item.get("status") or ""),
            "started_at": (_apify_time(item.get("startedAt")) or datetime.now(UTC).replace(tzinfo=None)).isoformat(),
            "finished_at": (_apify_time(item.get("finishedAt")).isoformat() if item.get("finishedAt") else None),
            "item_count": count, "origin": str((item.get("meta") or {}).get("origin") or ""),
            "local_run_id": tracked.id if tracked else None, "imported": bool(tracked and tracked.completed_at),
            "importable": not (tracked and tracked.completed_at),
        })
    return out


def import_remote_run(db: Session, external_run_id: str, reimport: bool = False) -> dict[str, Any]:
    """Track any run in the user's Apify account by ID and import its dataset (normalized and deduplicated)."""
    owner = require_user_id(db)
    token = apify_token(db, owner)
    search_run = db.scalar(select(SearchRun).where(SearchRun.user_id == owner, SearchRun.external_run_id == external_run_id))
    if not search_run:
        run = (apify_get(f"/actor-runs/{external_run_id}", token=token, operation="actor_run_status",
                         metadata={"run_id": external_run_id}) or {}).get("data") or {}
        if not run:
            raise ValueError(f"Apify run {external_run_id} was not found")
        actor_id, _ = _actor_name(str(run.get("actId") or ""), {}, token)
        actor_input: dict[str, Any] = {}
        if run.get("defaultKeyValueStoreId"):
            try:
                record = apify_get(f"/key-value-stores/{run['defaultKeyValueStoreId']}/records/INPUT",
                                   token=token, operation="run_input_fetch", metadata={"run_id": external_run_id})
                actor_input = record if isinstance(record, dict) else {}
            except (httpx.HTTPError, ValueError):
                actor_input = {}
        search_run = SearchRun(user_id=owner, provider="apify", external_run_id=external_run_id, actor_id=actor_id,
                               input_json=actor_input, status=str(run.get("status") or "READY"),
                               dataset_id=str(run.get("defaultDatasetId") or ""),
                               created_at=_apify_time(run.get("startedAt")) or datetime.now(UTC).replace(tzinfo=None))
        db.add(search_run)
        db.commit()
        db.refresh(search_run)
    return refresh_apify_run(db, search_run, reimport=reimport)


# Apify answers 403 when the saved token may not see a run (it was started with another token or account) and 404
# when the run or its dataset no longer exists. Neither changes on a retry, so the run is given up on at once.
RUN_GONE_CODES = {403: "Apify refused access to this run (HTTP 403): it was probably started with a different "
                       "Apify token or account than the one saved now",
                  404: "Apify no longer has this run or its dataset (HTTP 404)"}


def give_up_on_run(db: Session, search_run: SearchRun, reason: str) -> dict[str, Any]:
    """Mark a run that can never be imported as finished-and-failed, so the scheduler stops checking it. Logs it once.
    Commits."""
    search_run.status = "FAILED"
    search_run.error = reason[:2000]
    search_run.completed_at = search_run.completed_at or datetime.now(UTC).replace(tzinfo=None)
    if search_run.adapter:  # a Hunter run keeps its own status too
        from app.services.hunter import FAILED as HUNTER_FAILED

        search_run.hunter_status = HUNTER_FAILED
    db.commit()
    system_log.log_event(category="APIFY", level="WARNING", provider="APIFY", operation="actor_run_status",
                         message=f"Stopped checking Apify run {search_run.external_run_id}: {reason}",
                         status="GAVE_UP", user_id=search_run.user_id, entity_type="search_run",
                         entity_id=search_run.id, details={"actor_id": search_run.actor_id})
    return {"id": search_run.id, "provider": search_run.provider, "external_run_id": search_run.external_run_id,
            "actor_id": search_run.actor_id, "status": search_run.status, "completed": True, "gave_up": True,
            "error": search_run.error, "import": None}


def refresh_apify_run(db: Session, search_run: SearchRun, reimport: bool = False) -> dict[str, Any]:
    """Update a run's status and import its dataset once it finishes; reimport pulls it again.

    Uses the Apify token of the account that owns the run, so the scheduler can sync every account's runs. A run
    Apify refuses (403) or no longer has (404) is given up on instead of being checked again forever.
    """
    try:
        return _refresh_run(db, search_run, reimport=reimport)
    except httpx.HTTPStatusError as exc:
        reason = RUN_GONE_CODES.get(exc.response.status_code)
        if reason is None:
            raise
        db.rollback()
        run = db.get(SearchRun, search_run.id)
        return give_up_on_run(db, run, reason) if run is not None else {"id": search_run.id, "gave_up": True}


def _refresh_run(db: Session, search_run: SearchRun, reimport: bool = False) -> dict[str, Any]:
    if search_run.provider != "apify":
        raise ValueError("Search run is not an Apify run")
    if search_run.adapter:  # a Hunter run: its actor's adapter normalizes and deduplicates the dataset
        from app.services.hunter import refresh_hunter_run

        return refresh_hunter_run(db, search_run, reimport=reimport)
    if search_run.user_id is None:
        raise RuntimeError("This search run does not belong to an account")
    token = apify_token(db, search_run.user_id)
    if not token:
        raise RuntimeError("The account that started this search has no Apify API token")

    with tracing.trace("apify_sync", workflow_type="system", user_id=search_run.user_id):
        return _refresh_apify_run(db, search_run, token, reimport=reimport)


def _refresh_apify_run(db: Session, search_run: SearchRun, token: str, reimport: bool = False) -> dict[str, Any]:
    settings = get_settings()
    if search_run.actor_id and "/" not in search_run.actor_id:
        # An imported run whose actor name could not be looked up keeps Apify's opaque ID. Its items are only
        # imported once the name is known: the wrong item mapping would store jobs that a later pull cannot fix.
        search_run.actor_id, _ = _actor_name(search_run.actor_id, {}, token)
        if "/" not in search_run.actor_id:
            db.commit()
            raise RuntimeError("Could not look up this run's actor on Apify; its results will be imported on the next try")
    run_meta = {"run_id": search_run.external_run_id, "actor_id": search_run.actor_id,
                "saved_search_id": search_run.saved_search_id, "search_profile_id": search_run.search_profile_id}
    run = (apify_get(f"/actor-runs/{search_run.external_run_id}", token=token, operation="actor_run_status",
                     metadata=run_meta) or {}).get("data") or {}
    status = str(run.get("status") or search_run.status)
    search_run.status = status
    search_run.dataset_id = str(run.get("defaultDatasetId") or search_run.dataset_id or "")
    apply_apify_usage(search_run, run)

    import_stats = None
    finished = status in APIFY_TERMINAL_STATES
    if finished and (search_run.completed_at is None or reimport):
        if status == "SUCCEEDED" and not search_run.dataset_id:
            raise RuntimeError("Apify run succeeded but no default dataset was returned")
        items: list = []
        if search_run.dataset_id:
            try:
                payload = apify_get(f"/datasets/{search_run.dataset_id}/items",
                                    {"format": "json", "clean": "true", "limit": settings.apify_dataset_limit},
                                    token=token, operation="dataset_fetch",
                                    metadata={**run_meta, "dataset_id": search_run.dataset_id})
                items = payload.get("items", []) if isinstance(payload, dict) else payload
            except httpx.HTTPError:
                if status == "SUCCEEDED":
                    raise
                items = []  # failed runs may have no dataset; keep the error below
        if not isinstance(items, list):
            items = []
        from app.services.actors import default_site_for, item_normalizer

        # Failed, aborted and timed-out runs still import whatever they collected.
        import_stats = import_apify_items(db, items, search_run.id, default_site_for(search_run.actor_id),
                                          user_id=search_run.user_id, adapt=item_normalizer(search_run.actor_id))
        search_run.item_count = import_stats["seen"]
        search_run.created_jobs = db.scalar(select(func.count(Job.id)).where(Job.search_run_id == search_run.id)) or 0
        # What the run actually produced, for the Apify tab: found → normalized → kept after deduplication.
        search_run.stats = {**(search_run.stats or {}), "import": import_stats}
        if status != "SUCCEEDED":
            search_run.error = str(run.get("statusMessage") or run.get("errorMessage") or status)[:2000]
        search_run.completed_at = search_run.completed_at or datetime.now(UTC).replace(tzinfo=None)
        system_log.log_event(
            category="APIFY", level="INFO" if status == "SUCCEEDED" else "WARNING",
            message=f"Apify run {search_run.external_run_id} {status.lower()}: "
                    f"{import_stats['seen']} found, {import_stats.get('created', 0)} new",
            provider="APIFY", operation="dataset_import", status=status, user_id=search_run.user_id,
            entity_type="search_run", entity_id=search_run.id,
            details={"actor_id": search_run.actor_id, **import_stats,
                     "compute_units": search_run.compute_units, "usage_total_usd": _float(search_run.usage_total_usd)})

    db.commit()
    db.refresh(search_run)
    return {
        "id": search_run.id,
        "provider": search_run.provider,
        "external_run_id": search_run.external_run_id,
        "actor_id": search_run.actor_id,
        "status": search_run.status,
        "dataset_id": search_run.dataset_id,
        "item_count": search_run.item_count,
        "created_jobs": search_run.created_jobs,
        "completed": search_run.status in APIFY_TERMINAL_STATES,
        "error": search_run.error,
        "import": import_stats,
    }
