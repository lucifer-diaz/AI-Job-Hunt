"""The interactive setup guide: progress through the setup quests, and the guide's own small state."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_user_db
from app.services import setup_guide

router = APIRouter()


class GuideStateIn(BaseModel):
    dismissed: bool | None = None  # hides the floating quest tracker
    welcomed: bool | None = None  # the welcome screen was seen; it opens by itself only before that
    # Quests whose completion the guide has celebrated; only ever added to.
    celebrated: list[str] = Field(default_factory=list, max_length=50)


@router.get("/api/setup/guide")
def guide_get(db: Session = Depends(get_user_db)):
    return setup_guide.progress(db, db.info["user_id"])


@router.put("/api/setup/guide")
def guide_put(payload: GuideStateIn, db: Session = Depends(get_user_db)):
    setup_guide.save_state(db, db.info["user_id"], dismissed=payload.dismissed, welcomed=payload.welcomed,
                          celebrated=payload.celebrated)
    return setup_guide.progress(db, db.info["user_id"])
