from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, SearchRun
from app.settings import get_settings


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
) -> tuple[Job, bool]:
    existing = db.scalar(select(Job).where(Job.source == source, Job.source_ref == source_ref))
    if existing:
        if description and len(description) > len(existing.description or ""):
            existing.description = description
        if url:
            existing.url = url
        if raw:
            existing.raw = raw
        db.commit()
        return existing, False
    job = Job(
        source=source,
        source_ref=source_ref,
        title=(title or "Untitled")[:300],
        company=(company or "Unknown company")[:300],
        location=(location or "")[:300],
        url=url or "",
        description=description or "",
        raw=raw or {},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, True


def import_lever(db: Session, site: str) -> dict[str, int]:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    with httpx.Client(timeout=30, headers={"User-Agent": UA}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
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
    return {"seen": len(data), "created": created}


def import_greenhouse(db: Session, board_token: str) -> dict[str, int]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    with httpx.Client(timeout=30, headers={"User-Agent": UA}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json().get("jobs", [])
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
    return {"seen": len(data), "created": created}


def fetch_job_page(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as client:
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

    return {
        "source": f"apify:{site}",
        "source_ref": source_ref,
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "description": description,
        "raw": item,
    }


def import_apify_items(db: Session, items: list[dict[str, Any]], search_run_id: int | None = None,
                       default_site: str = "unknown") -> dict[str, int]:
    created = 0
    normalized = 0
    skipped = 0
    for item in items:
        job_data = normalize_apify_job(item, default_site)
        if not job_data:
            skipped += 1
            continue
        normalized += 1
        job, is_new = upsert_job(db, **job_data)
        if is_new and search_run_id is not None:
            job.search_run_id = search_run_id
            db.commit()
        created += int(is_new)
    return {"seen": len(items), "normalized": normalized, "created": created, "skipped": skipped}


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


def start_apify_search(db: Session, payload: dict[str, Any]) -> SearchRun:
    settings = get_settings()
    if not settings.apify_api_token:
        raise RuntimeError("APIFY_API_TOKEN is missing")

    actor_id = (payload.get("actor_id") or settings.apify_actor_id).strip()
    # Search profiles pass an input already built for this actor's schema.
    actor_input = payload.get("actor_input") or build_apify_input(payload)
    api_actor_id = _apify_actor_api_id(actor_id)
    headers = {
        "Authorization": f"Bearer {settings.apify_api_token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    with httpx.Client(timeout=30, headers=headers) as client:
        resp = client.post(f"https://api.apify.com/v2/actors/{api_actor_id}/runs", json=actor_input)
        resp.raise_for_status()
        run = (resp.json() or {}).get("data") or {}

    external_id = str(run.get("id") or "")
    if not external_id:
        raise RuntimeError("Apify did not return a run ID")

    search_run = SearchRun(
        provider="apify",
        external_run_id=external_id,
        actor_id=actor_id,
        input_json=actor_input,
        status=str(run.get("status") or "READY"),
        dataset_id=str(run.get("defaultDatasetId") or ""),
    )
    db.add(search_run)
    db.commit()
    db.refresh(search_run)
    return search_run


def refresh_apify_run(db: Session, search_run: SearchRun) -> dict[str, Any]:
    settings = get_settings()
    if search_run.provider != "apify":
        raise ValueError("Search run is not an Apify run")
    if not settings.apify_api_token:
        raise RuntimeError("APIFY_API_TOKEN is missing")

    headers = {"Authorization": f"Bearer {settings.apify_api_token}", "User-Agent": UA}
    with httpx.Client(timeout=30, headers=headers) as client:
        resp = client.get(f"https://api.apify.com/v2/actor-runs/{search_run.external_run_id}")
        resp.raise_for_status()
        run = (resp.json() or {}).get("data") or {}

        status = str(run.get("status") or search_run.status)
        search_run.status = status
        search_run.dataset_id = str(run.get("defaultDatasetId") or search_run.dataset_id or "")

        import_stats = None
        if status == "SUCCEEDED" and search_run.completed_at is None:
            if not search_run.dataset_id:
                raise RuntimeError("Apify run succeeded but no default dataset was returned")
            items_resp = client.get(
                f"https://api.apify.com/v2/datasets/{search_run.dataset_id}/items",
                params={"format": "json", "clean": "true", "limit": settings.apify_dataset_limit},
            )
            items_resp.raise_for_status()
            payload = items_resp.json()
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                items = []
            from app.services.actors import default_site_for

            import_stats = import_apify_items(db, items, search_run.id, default_site_for(search_run.actor_id))
            search_run.item_count = import_stats["seen"]
            search_run.created_jobs = import_stats["created"]
            search_run.completed_at = datetime.now(UTC).replace(tzinfo=None)
        elif status in {"FAILED", "ABORTED", "TIMED-OUT"} and search_run.completed_at is None:
            search_run.error = str((run.get("statusMessage") or run.get("errorMessage") or status))[:2000]
            search_run.completed_at = datetime.now(UTC).replace(tzinfo=None)

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
