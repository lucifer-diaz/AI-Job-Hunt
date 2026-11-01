"""Settings → AI agents & automation: the LangGraph switch and graph modes, the application worker's mode, the
browser-use options, checkpoint retention and LangSmith export (app.services.server_settings.CONFIG).

    GET /api/settings/automation   every setting with its choices, the value in force and the environment's; the
                                   presets; what only the environment decides (whether a key or a path is set, never
                                   its value); and whether the viewer may change them
    PUT /api/settings/automation   {values: {name: value}} — an admin only; all or none; takes effect without a restart

Server-wide: these steer every account's background work, so only an admin changes them; everyone may read them.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user, get_user_db
from app.services import server_settings

router = APIRouter(prefix="/api/settings/automation", tags=["settings"])


def _view(db: Session, user: CurrentUser) -> dict[str, Any]:
    out = server_settings.describe_config()
    out["editable"] = bool(user.is_admin)
    out["environment"]["postgres"] = db.get_bind().dialect.name == "postgresql"
    return out


@router.get("")
def automation(db: Session = Depends(get_user_db), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    return _view(db, user)


class AutomationIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.put("")
def update_automation(payload: AutomationIn, db: Session = Depends(get_user_db),
                      user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    if not user.is_admin:
        raise HTTPException(403, "Only an admin can change the server's automation settings")
    try:
        server_settings.update_config(db, payload.values, actor=f"user:{user.id}")
    except server_settings.ServerSettingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _view(db, user)
