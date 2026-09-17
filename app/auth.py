"""Accounts and sign-in sessions.

People sign up with an email address, a password and the password again, then sign in with email and
password. Passwords are hashed with scrypt. A session is a random token in an HttpOnly, SameSite=Lax
cookie; only its SHA-256 hash is stored, so a copy of the database contains no usable sessions.

The first account ever created becomes the admin and takes ownership of everything that existed before
accounts were introduced, including the server's .env API keys (stored encrypted on that account).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import AppSetting, CandidateProfile, User, UserSession
from app.security import DUMMY_PASSWORD_HASH, encrypt_secret, hash_password, verify_password
from app.services.user_settings import KEY_COLUMNS
from app.settings import get_settings
from app.tenancy import OWNED_MODELS

SESSION_COOKIE = "jhos_session"
PASSWORD_MIN_LENGTH = 10
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
# PostgreSQL advisory lock: two simultaneous first sign-ups must not both become the admin.
SIGNUP_LOCK_KEY = 7_220_310_002

router = APIRouter()


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    is_admin: bool

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "is_admin": self.is_admin}


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)
    confirm_password: str = Field(max_length=256)

    @model_validator(mode="after")
    def passwords_are_usable(self) -> SignupIn:
        if len(self.password) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256, repr=False)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=256, repr=False)
    confirm_password: str = Field(max_length=256, repr=False)

    @model_validator(mode="after")
    def passwords_match(self) -> PasswordChangeIn:
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        if self.new_password == self.current_password:
            raise ValueError("Choose a different new password")
        return self


class AccountExists(ValueError):
    pass


def normalize_email(value: str) -> str:
    return value.strip().lower()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ───────────── accounts ─────────────

def claim_existing_data(db: Session, user_id: int) -> None:
    """Give the first account the data created before accounts existed, and the server's .env API keys."""
    for model in OWNED_MODELS:
        conditions = [model.user_id.is_(None)]
        if model in (CandidateProfile, AppSetting):  # one of each per user: keep the original row
            first_id = db.scalar(select(func.min(model.id)).where(model.user_id.is_(None)))
            if first_id is None:
                continue
            conditions.append(model.id == first_id)
        db.execute(update(model).where(*conditions).values(user_id=user_id).execution_options(synchronize_session=False))

    settings = get_settings()
    env_keys = {"openrouter": settings.openrouter_api_key, "openai": settings.openai_api_key,
                "anthropic": settings.anthropic_api_key, "gemini": settings.gemini_api_key,
                "apify": settings.apify_api_token}
    if not settings.secret_key or not any(env_keys.values()):
        return
    row = db.scalar(select(AppSetting).where(AppSetting.user_id == user_id))
    if row is None:
        row = AppSetting(user_id=user_id, llm_provider=settings.llm_provider,
                         model=getattr(settings, f"{settings.llm_provider}_model", "") or "")
        db.add(row)
    for name, value in env_keys.items():
        if value and not getattr(row, KEY_COLUMNS[name]):
            setattr(row, KEY_COLUMNS[name], encrypt_secret(value))


def register_user(db: Session, email: str, password: str) -> User:
    email = normalize_email(email)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SIGNUP_LOCK_KEY})
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        db.rollback()
        raise AccountExists(email)
    first = (db.scalar(select(func.count(User.id))) or 0) == 0
    user = User(email=email, password_hash=hash_password(password), is_admin=first)
    db.add(user)
    try:
        db.flush()
        if first:
            claim_existing_data(db, user.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccountExists(email) from exc
    db.refresh(user)
    return user


# ───────────── sessions ─────────────

def start_session(db: Session, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(UserSession(user_id=user_id, token_hash=_token_hash(token), created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(days=get_settings().session_days)))
    db.commit()
    return token


def _start_verified_session(db: Session, user: User) -> str:
    """Hold the account write lock from hash recheck through session creation."""
    changed = db.execute(update(User).where(User.id == user.id, User.password_hash == user.password_hash)
                         .values(last_login_at=utcnow()))
    if changed.rowcount != 1:
        raise HTTPException(401, "Email or password is incorrect")
    # start_session commits both this conditional update and the new session.
    return start_session(db, user.id)


def session_user(request: Request) -> CurrentUser | None:
    """The user whose valid session cookie came with this request."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        return None
    with SessionLocal() as db:
        row = db.execute(select(UserSession, User).join(User, User.id == UserSession.user_id)
                         .where(UserSession.token_hash == _token_hash(token))).first()
        if row is None:
            return None
        session, user = row
        now = utcnow()
        if session.expires_at <= now:
            db.delete(session)
            db.commit()
            return None
        if now - session.last_seen_at > timedelta(minutes=5):  # keep writes rare
            session.last_seen_at = now
            db.commit()
        return CurrentUser(user.id, user.email, user.is_admin)


# The request middleware calls this; tests swap it to act as a signed-in user without cookies.
authenticate = session_user


def current_user(request: Request) -> CurrentUser:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


def get_user_db(user: CurrentUser = Depends(current_user)):
    """A database session that can only see and change the signed-in user's data (app.tenancy)."""
    db = SessionLocal()
    db.info["user_id"] = user.id
    try:
        yield db
    finally:
        db.close()


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    secure = get_settings().session_cookie_secure
    if secure is None:
        secure = request.url.scheme == "https"
    response.set_cookie(SESSION_COOKIE, token, max_age=get_settings().session_days * 86400, httponly=True,
                        samesite="lax", secure=secure, path="/")


# ───────────── sign-in throttling ─────────────

_failures: dict[str, deque[float]] = defaultdict(deque)
_failures_lock = threading.Lock()


def _recent_failures(key: str, now: float) -> int:
    attempts = _failures[key]
    while attempts and now - attempts[0] > LOGIN_FAILURE_WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts)


# ───────────── routes ─────────────

@router.post("/api/auth/signup", status_code=201)
def signup(payload: SignupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not get_settings().allow_signup:
        raise HTTPException(403, "Sign-up is closed on this server")
    try:
        user = register_user(db, payload.email, payload.password)
    except AccountExists as exc:
        raise HTTPException(409, "An account with this email already exists. Sign in instead.") from exc
    _set_session_cookie(response, request, start_session(db, user.id))
    return {"user": CurrentUser(user.id, user.email, user.is_admin).public()}


@router.post("/api/auth/login")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email, now = normalize_email(payload.email), time.monotonic()
    with _failures_lock:
        if _recent_failures(email, now) >= LOGIN_FAILURE_LIMIT:
            raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.email == email))
    password_ok = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not password_ok:
        with _failures_lock:
            _failures[email].append(now)
        raise HTTPException(401, "Email or password is incorrect")
    token = _start_verified_session(db, user)
    with _failures_lock:
        _failures.pop(email, None)
    _set_session_cookie(response, request, token)
    return {"user": CurrentUser(user.id, user.email, user.is_admin).public()}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: CurrentUser = Depends(current_user)):
    return {"user": user.public()}


def _check_current_password(user: User, password: str) -> None:
    key, now = f"password:{user.id}", time.monotonic()
    with _failures_lock:
        if _recent_failures(key, now) >= LOGIN_FAILURE_LIMIT:
            raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    if not verify_password(password, user.password_hash):
        with _failures_lock:
            _failures[key].append(time.monotonic())
        raise HTTPException(403, "Current password is incorrect")


def _replace_password_session(db: Session, user: User, password: str) -> str:
    """Compare-and-swap also serializes competing changes on SQLite."""
    changed = db.execute(update(User).where(User.id == user.id, User.password_hash == user.password_hash)
                         .values(password_hash=hash_password(password)))
    if changed.rowcount != 1:
        raise HTTPException(409, "Password changed in another request. Sign in again.")
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    token, now = secrets.token_urlsafe(32), utcnow()
    db.add(UserSession(user_id=user.id, token_hash=_token_hash(token), created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(days=get_settings().session_days)))
    return token


@router.post("/api/auth/password")
def change_password(payload: PasswordChangeIn, request: Request, response: Response,
                    identity: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.id == identity.id).with_for_update())
    if user is None:
        raise HTTPException(401, "Sign in required")
    _check_current_password(user, payload.current_password)
    token = _replace_password_session(db, user, payload.new_password)
    db.commit()  # hash, revocation and replacement succeed or roll back together
    with _failures_lock:
        _failures.pop(f"password:{user.id}", None)
    _set_session_cookie(response, request, token)
    return {"ok": True}
