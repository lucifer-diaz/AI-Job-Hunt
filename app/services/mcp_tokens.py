"""Personal access tokens for the local Claude Desktop MCP server (app.mcp).

A token says which account the MCP server acts for. Only its SHA-256 is stored, the plaintext is shown once,
and a revoked token stops working at once. Tokens grant only the MCP tools, never the web app.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import McpToken, User
from app.tenancy import require_user_id

TOKEN_PREFIX = "jhos_mcp_"


class InvalidToken(Exception):
    """A missing, malformed, unknown or revoked MCP token."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_dict(row: McpToken) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "created_at": row.created_at.isoformat(),
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "revoked": row.revoked_at is not None}


def create_token(db: Session, name: str = "Claude Desktop", user_id: int | None = None) -> tuple[McpToken, str]:
    """A new token for the session's user (or `user_id` for the CLI). Returns the row and the one-time plaintext."""
    owner = user_id if user_id is not None else require_user_id(db)
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = McpToken(user_id=owner, name=name.strip()[:100] or "Claude Desktop", token_hash=token_hash(plaintext))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, plaintext


def list_tokens(db: Session) -> list[dict[str, Any]]:
    return [token_dict(t) for t in db.scalars(select(McpToken).order_by(McpToken.id)).all()]


def revoke_token(db: Session, token_id: int) -> bool:
    row = db.get(McpToken, token_id)
    if row is None:
        return False
    row.revoked_at = row.revoked_at or utcnow()
    db.commit()
    return True


def resolve_token(db: Session, token: str | None) -> int:
    """The account a token belongs to. `db` must be a system session (no user yet)."""
    token = (token or "").strip()
    if not token.startswith(TOKEN_PREFIX):
        raise InvalidToken("JOBHUNT_MCP_TOKEN is missing or is not a Job Hunt OS MCP token")
    row = db.scalar(select(McpToken).where(McpToken.token_hash == token_hash(token)))
    if row is None or row.revoked_at is not None or db.get(User, row.user_id) is None:
        raise InvalidToken("JOBHUNT_MCP_TOKEN is unknown or has been revoked; create a new one in Job Hunt OS")
    row.last_used_at = utcnow()
    db.commit()
    return row.user_id
