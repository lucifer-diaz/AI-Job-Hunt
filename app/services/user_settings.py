"""Each user's candidate profile, app settings and API keys.

API keys are stored encrypted (app.security) and only ever used for that user's own work: their
searches, ranking, tailoring and follow-ups. There is no fallback to server-wide keys.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AppSetting, CandidateProfile
from app.security import decrypt_secret, encrypt_secret
from app.services.ai_config import resolve as resolve_ai_config
from app.settings import get_settings
from app.tenancy import require_user_id

KEY_COLUMNS = {"openrouter": "openrouter_api_key_enc", "openai": "openai_api_key_enc",
               "anthropic": "anthropic_api_key_enc", "gemini": "gemini_api_key_enc", "apify": "apify_api_token_enc"}
KEY_LABELS = {"openrouter": "OpenRouter", "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Gemini",
              "apify": "Apify"}

Row = TypeVar("Row", CandidateProfile, AppSetting)


def lock_settings_row(db: Session, row: Row) -> Row:
    """Reload after taking a write lock, including SQLite's single-writer lock."""
    model = type(row)
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(model).where(model.id == row.id).values(id=row.id))
    return db.scalar(select(model).where(model.id == row.id).with_for_update()
                     .execution_options(populate_existing=True))


def _owner(db: Session, user_id: int | None) -> int:
    return user_id if user_id is not None else require_user_id(db)


def _get_or_create(db: Session, row_type: type[Row], user_id: int, **defaults: Any) -> Row:
    row = db.scalar(select(row_type).where(row_type.user_id == user_id))
    if row is not None:
        return row
    try:
        row = row_type(user_id=user_id, **defaults)
        db.add(row)
        db.commit()
    except IntegrityError:  # created by a concurrent request for the same user
        db.rollback()
        row = db.scalar(select(row_type).where(row_type.user_id == user_id))
        if row is None:  # a different constraint failed; don't hide it behind a None row
            raise
    return row


def profile_for(db: Session, user_id: int | None = None) -> CandidateProfile:
    return _get_or_create(db, CandidateProfile, _owner(db, user_id))


def settings_for(db: Session, user_id: int | None = None) -> AppSetting:
    settings = get_settings()
    return _get_or_create(db, AppSetting, _owner(db, user_id), llm_provider=settings.llm_provider,
                          model=getattr(settings, f"{settings.llm_provider}_model", "") or "")


# Who does the tailoring and review reasoning: the configured model API (backend workers) or Claude Desktop over MCP.
EXECUTION_MODES = ("backend", "claude_desktop")


def execution_mode_for(db: Session, user_id: int | None = None) -> str:
    mode = (settings_for(db, user_id).preferences or {}).get("execution_mode")
    return mode if mode in EXECUTION_MODES else "backend"


# How much work Job Hunt OS may do for one account at a time. The server sets the ceiling (RANKING_CONCURRENCY,
# TAILOR_CONCURRENCY); an account may run at or below it, so one busy account cannot take the whole machine.
PROCESSING_LIMITS = ("ranking_concurrency", "tailoring_concurrency")
# How long an account waits between ranking runs of its own. 0 means "whenever the server looks", which is
# every RANKING_INTERVAL_MINUTES; a larger number spreads the same work over a longer stretch.
MAX_RANKING_INTERVAL_MINUTES = 1440


def ranking_interval_minutes(db: Session, user_id: int | None = None) -> int:
    saved = (settings_for(db, user_id).preferences or {}).get("processing") or {}
    try:
        return max(0, min(MAX_RANKING_INTERVAL_MINUTES, int(saved.get("ranking_interval_minutes", 0))))
    except (TypeError, ValueError):
        return 0


def processing_maximums() -> dict[str, int]:
    """The server's ceilings — the most an account is allowed to ask for."""
    settings = get_settings()
    return {"ranking_concurrency": max(1, settings.ranking_concurrency),
            "tailoring_concurrency": max(1, settings.tailor_concurrency)}


def _bounded(value: Any, ceiling: int) -> int:
    try:
        return max(1, min(int(value), ceiling))
    except (TypeError, ValueError):
        return ceiling


def processing_limits(db: Session, user_id: int | None = None) -> dict[str, int]:
    """How many ranking and tailoring tasks may run at once for this account, and how often ranking runs."""
    saved = (settings_for(db, user_id).preferences or {}).get("processing") or {}
    ceilings = processing_maximums()
    return {**{name: _bounded(saved.get(name), ceilings[name]) for name in PROCESSING_LIMITS},
            "ranking_interval_minutes": ranking_interval_minutes(db, user_id)}


def limits_for(db: Session, user_ids: Iterable[int], name: str) -> dict[int, int]:
    """One limit for several accounts at once, for the workers that serve every account in one pass."""
    ceiling = processing_maximums()[name]
    rows = db.execute(select(AppSetting.user_id, AppSetting.preferences)
                      .where(AppSetting.user_id.in_(list(dict.fromkeys(user_ids))))).all()
    saved = {user_id: ((preferences or {}).get("processing") or {}).get(name) for user_id, preferences in rows}
    return {user_id: _bounded(saved.get(user_id), ceiling) for user_id in dict.fromkeys(user_ids)}


def ai_overrides(db: Session, user_id: int | None = None) -> dict[str, Any]:
    return dict((settings_for(db, user_id).preferences or {}).get("ai") or {})


def user_keys(db: Session, user_id: int | None = None) -> dict[str, str | None]:
    row = settings_for(db, user_id)
    return {name: decrypt_secret(getattr(row, column)) for name, column in KEY_COLUMNS.items()}


def apify_token(db: Session, user_id: int | None = None) -> str | None:
    return user_keys(db, user_id)["apify"]


def key_status(db: Session, user_id: int | None = None) -> dict[str, dict[str, Any]]:
    """Which keys a user has saved, with only the last four characters as a reminder."""
    return {name: {"configured": bool(value), "hint": f"…{value[-4:]}" if value and len(value) >= 8 else ""}
            for name, value in user_keys(db, user_id).items()}


def set_user_keys(db: Session, updates: dict[str, str | None], user_id: int | None = None) -> None:
    """Save keys: None leaves a key unchanged, an empty string removes it. Raises SecretsUnavailable."""
    row = settings_for(db, user_id)
    for name, value in updates.items():
        if name not in KEY_COLUMNS or value is None:
            continue
        clean = value.strip()
        setattr(row, KEY_COLUMNS[name], encrypt_secret(clean) if clean else None)
    db.commit()


def ai_ready(db: Session, user_id: int) -> tuple[bool, str]:
    """Whether this account's chosen AI provider can be called, so workers never burn attempts on a missing key."""
    provider = resolve_ai_config(get_settings(), ai_overrides(db, user_id))["llm_provider"]
    if provider == "mock" or user_keys(db, user_id).get(provider):
        return True, ""
    return False, f"No {KEY_LABELS.get(provider, provider)} API key saved in Settings"


# CandidatePolicy: how AI workflows may use resume facts the user has not verified.
CANDIDATE_POLICY_DEFAULTS = {
    # Tailoring, review, follow-ups and application answers: verified facts only unless this is on.
    "allow_unverified_resume_facts": False,
    # Ranking may read unverified parsed facts as clearly marked, lower-confidence context.
    "ranking_uses_unverified": True,
}


def candidate_policy(db: Session, user_id: int | None = None) -> dict[str, bool]:
    stored = (settings_for(db, user_id).preferences or {}).get("candidate_policy") or {}
    return {key: bool(stored.get(key, default)) for key, default in CANDIDATE_POLICY_DEFAULTS.items()}
