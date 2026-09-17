"""Each user's candidate profile, app settings and API keys.

API keys are stored encrypted (app.security) and only ever used for that user's own work: their
searches, ranking, tailoring and follow-ups. There is no fallback to server-wide keys.
"""
from __future__ import annotations

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
