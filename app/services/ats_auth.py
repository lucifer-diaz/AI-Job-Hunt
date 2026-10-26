"""Saved ATS logins (Phase 6, ADR-035): a person's login sessions and an encrypted password vault, per ATS domain.

    credentials  save / delete / list (masked)        ← Settings → ATS logins; the password is write-only
    signup       begin_signup → the person signs up    ← the Apply kit, when a run stops at ACCOUNT_REQUIRED
                 with a generated password, then
                 save_login (PENDING → ACTIVE)
    sessions     save_session (from the takeover window, only on "Save this login"), session_state, revoke
    a run        login_secret(domain) → (username, password) for that domain's own login form only

Rules (AGENTS.md "Guarded submission and ATS logins"):
  * Nothing here is configuration: logins live in their own owned tables, never in worker settings, preferences or a
    snapshot. The password and the session are encrypted with SECRET_KEY (app.security) and decrypted only for a run
    filling that domain's login form, inside `redaction.holding`, so the value is scrubbed from any log or error.
  * No API returns a password, except once: the password generated for a signup, shown to the person so they can
    type it into the signup form. Job Hunt OS never reads a password field's value from a page.
  * A password is used only on a domain in APPLICATION_LOGIN_DOMAINS (empty by default), and a saved session only
    while APPLICATION_AUTH_SESSIONS_ENABLED. A session keeps only the cookies and storage of its own domain.
  * Signup is a person's act (it accepts a site's terms): no run ever creates an account.
"""
from __future__ import annotations

import json
import re
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AtsCredential, AtsSession
from app.security import decrypt_secret, encrypt_secret
from app.settings import get_settings

PENDING, ACTIVE = "PENDING", "ACTIVE"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
PASSWORD_LENGTH = 24
SYMBOLS = "!#$%*+-=?@_"


class AuthRefused(ValueError):
    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_domain(value: str | None) -> str:
    """A bare host name ("acme.wd5.myworkdayjobs.com"), or AuthRefused. No scheme, port, path or wildcard."""
    domain = (value or "").strip().lower().strip(".")
    if not DOMAIN_RE.match(domain):
        raise AuthRefused("A login belongs to one host name, such as acme.wd5.myworkdayjobs.com", "INVALID_DOMAIN")
    return domain


def mask(username: str) -> str:
    name, _, host = (username or "").partition("@")
    shown = (name[:2] + "•" * max(1, len(name) - 2)) if name else ""
    return f"{shown}@{host}" if host else shown


def generate_password() -> str:
    """A strong random password with every character class most sites ask for."""
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)
                and any(c in SYMBOLS for c in value)):
            return value


# ───────────── the vault ─────────────

def credential_view(row: AtsCredential) -> dict[str, Any]:
    """A saved login for screens: never the password."""
    return {"domain": row.domain, "username": mask(row.username), "status": row.status,
            "login_allowed": row.domain in get_settings().login_domains,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None}


def _credential(db: Session, user_id: int, domain: str) -> AtsCredential | None:
    return db.scalar(select(AtsCredential).where(AtsCredential.user_id == user_id, AtsCredential.domain == domain))


def credentials(db: Session, user_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(AtsCredential).where(AtsCredential.user_id == user_id)
                      .order_by(AtsCredential.domain).limit(500)).all()
    return [credential_view(r) for r in rows]


def save_credential(db: Session, user_id: int, domain: str, username: str, password: str) -> AtsCredential:
    """Store (or replace) the login for one domain, ACTIVE. Does not commit."""
    domain = normalize_domain(domain)
    username = (username or "").strip()
    if not username or len(username) > 320:
        raise AuthRefused("The username or email is required", "INVALID_USERNAME")
    if not password or len(password) > 500:
        raise AuthRefused("The password is required", "INVALID_PASSWORD")
    row = _credential(db, user_id, domain) or AtsCredential(user_id=user_id, domain=domain, created_at=utcnow())
    row.username, row.password_encrypted, row.status, row.updated_at = username, encrypt_secret(password), ACTIVE, \
        utcnow()
    db.add(row)
    db.flush()
    return row


def delete_credential(db: Session, user_id: int, domain: str) -> bool:
    row = _credential(db, user_id, normalize_domain(domain))
    if row is None:
        return False
    db.delete(row)
    return True


def begin_signup(db: Session, user_id: int, domain: str, email: str) -> tuple[AtsCredential, str]:
    """A pending login for a signup the person is about to make: their email and a generated password, which is
    returned this once so they can type it into the site's form. Refused when an active login is already saved (the
    person signs in instead). Does not commit."""
    domain = normalize_domain(domain)
    existing = _credential(db, user_id, domain)
    if existing is not None and existing.status == ACTIVE:
        raise AuthRefused("A login for this site is already saved: sign in with it instead", "ALREADY_SAVED")
    if not (email or "").strip():
        raise AuthRefused("The profile has no email to sign up with", "NO_EMAIL")
    password = generate_password()
    row = existing or AtsCredential(user_id=user_id, domain=domain, created_at=utcnow())
    row.username, row.password_encrypted, row.status, row.updated_at = email.strip(), encrypt_secret(password), \
        PENDING, utcnow()
    db.add(row)
    db.flush()
    return row, password


def save_login(db: Session, user_id: int, domain: str, *, username: str | None = None,
               password: str | None = None) -> AtsCredential:
    """The person finished signing up (or in): the pending login becomes active, or the username and password they
    typed are stored. Does not commit."""
    domain = normalize_domain(domain)
    row = _credential(db, user_id, domain)
    if password:
        return save_credential(db, user_id, domain, username or (row.username if row else ""), password)
    if row is None:
        raise AuthRefused("Type the password you used on the site to save this login", "NO_PASSWORD")
    if username:
        row.username = username.strip()
    row.status, row.updated_at = ACTIVE, utcnow()
    db.flush()
    return row


def discard_pending(db: Session, user_id: int, domain: str) -> bool:
    row = _credential(db, user_id, normalize_domain(domain))
    if row is None or row.status != PENDING:
        return False
    db.delete(row)
    return True


@dataclass(frozen=True)
class LoginSecret:
    domain: str
    username: str
    password: str

    def __repr__(self) -> str:  # never printed, logged or put in an exception by accident
        return f"LoginSecret(domain={self.domain!r}, username={mask(self.username)!r})"


def login_secret(db: Session, user_id: int, domain: str) -> LoginSecret | None:
    """The active login for exactly this domain, when the server lets logins be filled there. Use it inside
    `redaction.holding(secret.password)`. Does not commit (records last use)."""
    domain = normalize_domain(domain)
    if domain not in get_settings().login_domains:
        return None
    row = _credential(db, user_id, domain)
    if row is None or row.status != ACTIVE:
        return None
    password = decrypt_secret(row.password_encrypted)
    if not password:
        return None
    row.last_used_at = utcnow()
    return LoginSecret(domain=domain, username=row.username, password=password)


# ───────────── saved sessions ─────────────

def _matches(cookie_domain: str, domain: str) -> bool:
    host = (cookie_domain or "").lstrip(".").lower()
    return bool(host) and (domain == host or domain.endswith("." + host))


def scoped_state(state: dict[str, Any], domain: str) -> dict[str, Any]:
    """Only this domain's cookies and origins: a takeover window may have visited other sites (a single sign-on
    page, a search engine), and none of their session material is ever kept."""
    cookies = [c for c in state.get("cookies") or [] if isinstance(c, dict) and _matches(c.get("domain", ""), domain)]
    origins = [o for o in state.get("origins") or [] if isinstance(o, dict)
               and (o.get("origin") or "").lower().split("://")[-1].split(":")[0] == domain]
    return {"cookies": cookies, "origins": origins}


def save_session(db: Session, user_id: int, domain: str, state: dict[str, Any], *, ats: str | None = None,
                 now: datetime | None = None) -> AtsSession | None:
    """Keep a session the person logged in to (they clicked "Save this login"). Nothing is kept when the window
    holds no cookie of the domain. Does not commit."""
    domain = normalize_domain(domain)
    scoped = scoped_state(state or {}, domain)
    if not scoped["cookies"] and not scoped["origins"]:
        return None
    now = now or utcnow()
    row = db.scalar(select(AtsSession).where(AtsSession.user_id == user_id, AtsSession.domain == domain)) \
        or AtsSession(user_id=user_id, domain=domain, created_at=now)
    row.ats, row.storage_state_encrypted = ats, encrypt_secret(json.dumps(scoped))
    row.created_at, row.revoked_at = now, None
    row.expires_at = now + timedelta(days=max(1, get_settings().ats_session_max_days))
    db.add(row)
    db.flush()
    return row


def session_state(db: Session, user_id: int, domain: str) -> dict[str, Any] | None:
    """The saved session for this domain to start a run's browser with, when sessions are on and it is still
    valid. Does not commit (records last use)."""
    if not get_settings().application_auth_sessions_enabled:
        return None
    domain = normalize_domain(domain)
    row = db.scalar(select(AtsSession).where(AtsSession.user_id == user_id, AtsSession.domain == domain))
    if row is None or row.revoked_at is not None or row.expires_at <= utcnow():
        return None
    raw = decrypt_secret(row.storage_state_encrypted)
    if not raw:
        return None
    row.last_used_at = utcnow()
    return scoped_state(json.loads(raw), domain)


def revoke_session(db: Session, user_id: int, domain: str) -> bool:
    row = db.scalar(select(AtsSession).where(AtsSession.user_id == user_id,
                                             AtsSession.domain == normalize_domain(domain)))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    return True


def sessions(db: Session, user_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(AtsSession).where(AtsSession.user_id == user_id).order_by(AtsSession.domain)
                      .limit(500)).all()
    now = utcnow()
    return [{"domain": r.domain, "ats": r.ats, "active": r.revoked_at is None and r.expires_at > now,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "expires_at": r.expires_at.isoformat() if r.expires_at else None,
             "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
             "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None} for r in rows]
