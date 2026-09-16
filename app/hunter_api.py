"""Hunter API: start Apify searches, track their runs and keep reusable search profiles.

A search returns as soon as the actor runs are started. Results arrive later, when the scheduler's
Apify sync imports each finished run (app.services.hunter).
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.models import SearchProfile, SearchRun
from app.services.apify_adapters import ADAPTERS, DEFAULT_SITES, HUNTER_SITES, AdapterError, HunterSearch, clean_list
from app.services.hunter import (HUNTER_STATUSES, HunterConfigError, run_profile, run_summary, start_search,
                                 sync_pending_runs)
from app.tenancy import require_user_id

router = APIRouter(prefix="/api/hunter", tags=["hunter"])

JobType = Literal["", "fulltime", "parttime", "contract", "internship", "temporary"]


class HunterSearchIn(BaseModel):
    roles: list[str] = Field(min_length=1, max_length=25)
    locations: list[str] = Field(min_length=1, max_length=10)
    sites: list[str] = Field(default_factory=lambda: list(DEFAULT_SITES), min_length=1, max_length=6)
    hours_old: int | None = Field(default=72, ge=1, le=8760)
    max_results: int = Field(default=20, ge=1, le=100)
    remote_only: bool = False
    job_type: JobType = ""
    country: str = Field(default="", max_length=60)
    mode: Literal["primary", "fallback"] = "primary"
    require_posted_date: bool = False

    @field_validator("roles", "locations")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        cleaned = clean_list(values)
        if not cleaned:
            raise ValueError("add at least one non-blank value")
        if any(len(value) > 120 for value in cleaned):
            raise ValueError("each value must be 120 characters or fewer")
        return cleaned

    @field_validator("sites")
    @classmethod
    def _sites(cls, values: list[str]) -> list[str]:
        cleaned = [site.lower() for site in clean_list(values)]
        unknown = [site for site in cleaned if site not in HUNTER_SITES]
        if unknown or not cleaned:
            raise ValueError(f"choose sites from: {', '.join(HUNTER_SITES)}")
        return cleaned


class HunterProfileIn(HunterSearchIn):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    run_every_minutes: int = Field(default=1440, ge=30, le=10080)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = clean_list([value])
        if not cleaned:
            raise ValueError("give the profile a name")
        return cleaned[0]


def _search(payload: HunterSearchIn) -> HunterSearch:
    return HunterSearch.from_dict(payload.model_dump())


def _start(db: Session, search: HunterSearch, **kwargs: Any) -> dict[str, Any]:
    try:
        out = start_search(db, require_user_id(db), search, **kwargs)
    except HunterConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (ValueError, AdapterError) as exc:
        raise HTTPException(422, str(exc)) from exc
    runs = [run_summary(run) for run in out["runs"]]
    return {"runs": runs, "started": sum(run["status"] != "FAILED" for run in runs),
            "failed": sum(run["status"] == "FAILED" for run in runs), "warnings": out["warnings"]}


@router.get("/actors")
def hunter_actors():
    """The actors the Hunter can use and what each one supports."""
    return {"adapters": [adapter.public() for adapter in ADAPTERS.values()], "sites": list(HUNTER_SITES),
            "statuses": list(HUNTER_STATUSES)}


@router.post("/search", status_code=202)
def hunter_search(payload: HunterSearchIn, db: Session = Depends(get_user_db)):
    """Start the actor runs for a search and return at once; results are imported by the Apify sync."""
    return _start(db, _search(payload))


@router.get("/runs")
def hunter_runs(status: str | None = None, limit: int = 50, db: Session = Depends(get_user_db)):
    query = select(SearchRun).where(SearchRun.adapter.is_not(None))
    if status:
        query = query.where(SearchRun.hunter_status == status.upper())
    runs = db.scalars(query.order_by(desc(SearchRun.id)).limit(max(1, min(limit, 200)))).all()
    return {"runs": [run_summary(run) for run in runs]}


@router.post("/runs/sync")
def hunter_sync(db: Session = Depends(get_user_db)):
    """Check this account's unfinished runs now instead of waiting for the scheduler."""
    return sync_pending_runs(db, require_user_id(db))


@router.get("/runs/{run_id}")
def hunter_run(run_id: int, db: Session = Depends(get_user_db)):
    run = db.get(SearchRun, run_id)
    if not run or not run.adapter:
        raise HTTPException(404, "Hunter run not found")
    return run_summary(run)


def profile_out(profile: SearchProfile) -> dict[str, Any]:
    return {"id": profile.id, "name": profile.name, "roles": profile.roles, "locations": profile.locations,
            "sites": profile.sites, "hours_old": profile.hours_old, "max_results": profile.max_results,
            "remote_only": profile.remote_only, "job_type": profile.job_type, "country": profile.country,
            "mode": profile.mode, "require_posted_date": profile.require_posted_date, "enabled": profile.enabled,
            "run_every_minutes": profile.run_every_minutes,
            "last_run_at": profile.last_run_at.isoformat() if profile.last_run_at else None,
            "next_run_at": profile.next_run_at.isoformat() if profile.next_run_at else None}


def _get_profile(db: Session, profile_id: int) -> SearchProfile:
    profile = db.get(SearchProfile, profile_id)
    if not profile:
        raise HTTPException(404, "Search profile not found")
    return profile


def _save_profile(db: Session, profile: SearchProfile, payload: HunterProfileIn) -> SearchProfile:
    for field, value in payload.model_dump().items():
        setattr(profile, field, value)
    try:
        db.add(profile)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"You already have a search profile named '{payload.name}'") from exc
    db.refresh(profile)
    return profile


@router.get("/profiles")
def list_profiles(db: Session = Depends(get_user_db)):
    profiles = db.scalars(select(SearchProfile).order_by(SearchProfile.id)).all()
    return {"profiles": [profile_out(profile) for profile in profiles]}


@router.post("/profiles", status_code=201)
def create_profile(payload: HunterProfileIn, db: Session = Depends(get_user_db)):
    return profile_out(_save_profile(db, SearchProfile(user_id=require_user_id(db)), payload))


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, db: Session = Depends(get_user_db)):
    return profile_out(_get_profile(db, profile_id))


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: int, payload: HunterProfileIn, db: Session = Depends(get_user_db)):
    return profile_out(_save_profile(db, _get_profile(db, profile_id), payload))


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int, db: Session = Depends(get_user_db)):
    db.delete(_get_profile(db, profile_id))
    db.commit()
    return Response(status_code=204)


@router.post("/profiles/{profile_id}/run", status_code=202)
def run_profile_now(profile_id: int, db: Session = Depends(get_user_db)):
    profile = _get_profile(db, profile_id)
    try:
        out = run_profile(db, profile)
    except HunterConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (ValueError, AdapterError) as exc:
        raise HTTPException(422, str(exc)) from exc
    runs = [run_summary(run) for run in out["runs"]]
    return {"runs": runs, "started": sum(run["status"] != "FAILED" for run in runs),
            "failed": sum(run["status"] == "FAILED" for run in runs), "warnings": out["warnings"],
            "profile": profile_out(profile)}
