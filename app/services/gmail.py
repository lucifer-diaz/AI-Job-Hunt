"""Read-only Gmail for the Application Inbox: Google OAuth 2.0 and the few Gmail API reads it needs.

    Settings → Connect Gmail → Google consent (scope gmail.readonly) → /api/gmail/oauth/callback
      → the refresh token is encrypted with SECRET_KEY (app.security) and stored on GmailConnection
    each sync → a short-lived access token, in memory only → GmailClient (GET requests only)

Rules this module keeps:
* The only scope asked for is gmail.readonly, and the client only ever issues GET requests: nothing is sent, labelled,
  moved or deleted. A connection whose granted scopes lack gmail.readonly is refused.
* Credentials are never stored or logged: the refresh token is encrypted; access tokens, client secrets and
  Authorization headers stay in memory. Every call goes through app.services.external_api, which redacts.
* A revoked or expired grant marks the connection CONNECTION_REQUIRED (the person reconnects); it never raises out of a
  scheduled job and never affects another account or another worker.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Self
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GmailConnection
from app.security import decrypt_secret, encrypt_secret
from app.services.external_api import NO_CAPTURE, RateLimitedCooldown, external_client
from app.services.redaction import redact_text
from app.settings import get_settings

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
STATE_TTL_SECONDS = 900
METADATA_HEADERS = ("From", "To", "Subject", "Date", "List-Unsubscribe", "Auto-Submitted", "Precedence", "Reply-To")
CONNECTED, CONNECTION_REQUIRED, REVOKED, ERROR = "CONNECTED", "CONNECTION_REQUIRED", "REVOKED", "ERROR"
# Seconds to wait before retrying a Gmail request after a server error or timeout (tests set it to 0).
RETRY_SLEEP_SECONDS = 1.0
_transport: httpx.BaseTransport | None = None  # tests serve Google from an httpx.MockTransport


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Route Google calls through `transport` (tests). The instrumentation layer still records every call."""
    global _transport
    _transport = transport


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class GmailError(RuntimeError):
    """Gmail could not be read this time (outage, network). Nothing is lost; the next sync tries again."""


class GmailConnectionRequired(GmailError):
    """The grant is missing, expired, revoked or lacks gmail.readonly: the person has to connect Gmail again."""


class GmailRateLimited(GmailError):
    """Google said 429 (or this account is still backing off from one). The sync stops; the next one resumes."""


class GmailNotConfigured(GmailError):
    """GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set on this server."""


class GmailBadRequest(GmailError):
    """Gmail refused one request (HTTP 400: a query or id it cannot use). Only that request is skipped."""


def _client(provider: str, operation: str, user_id: int | None, **kwargs: Any):
    return external_client(provider, service="oauth2" if provider == "GOOGLE_OAUTH" else "gmail_v1",
                           operation=operation, user_id=user_id, timeout=20, transport=_transport, **kwargs)


# ───────────── OAuth ─────────────

def _require_config() -> Any:
    settings = get_settings()
    if not settings.gmail_oauth_configured:
        raise GmailNotConfigured("Gmail is not set up on this server (GOOGLE_OAUTH_CLIENT_ID and "
                                 "GOOGLE_OAUTH_CLIENT_SECRET)")
    return settings


def _sign(payload: str) -> str:
    key = (get_settings().secret_key or "").encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(user_id: int, now: datetime | None = None) -> str:
    """An opaque, signed OAuth state that names the account and expires: the callback checks it against the session."""
    expires = int(((now or utcnow()).replace(tzinfo=UTC)).timestamp()) + STATE_TTL_SECONDS
    payload = f"{user_id}.{expires}.{secrets.token_urlsafe(8)}"
    return f"{payload}.{_sign(payload)}"


def read_state(state: str, now: datetime | None = None) -> int:
    """The account a state was issued to. Raises ValueError for a forged, altered or expired state."""
    try:
        user_id, expires, nonce, signature = (state or "").split(".")
    except ValueError as exc:
        raise ValueError("invalid OAuth state") from exc
    payload = f"{user_id}.{expires}.{nonce}"
    if not hmac.compare_digest(_sign(payload), signature):
        raise ValueError("invalid OAuth state")
    if int(expires) < int(((now or utcnow()).replace(tzinfo=UTC)).timestamp()):
        raise ValueError("the Gmail connection request expired; start again")
    return int(user_id)


def authorization_url(user_id: int) -> str:
    settings = _require_config()
    params = {"client_id": settings.google_oauth_client_id, "redirect_uri": settings.gmail_redirect_uri,
              "response_type": "code", "scope": GMAIL_READONLY_SCOPE, "access_type": "offline",
              "prompt": "consent", "include_granted_scopes": "false", "state": make_state(user_id)}
    return f"{AUTH_URL}?{urlencode(params)}"


def connection_for(db: Session, user_id: int | None = None) -> GmailConnection | None:
    query = select(GmailConnection)
    if user_id is not None:
        query = query.where(GmailConnection.user_id == user_id)
    return db.scalar(query.limit(1))


def exchange_code(db: Session, user_id: int, code: str) -> GmailConnection:
    """Finish the consent flow: keep only the encrypted refresh token and the mailbox address."""
    settings = _require_config()
    with _client("GOOGLE_OAUTH", "exchange_code", user_id) as client:
        response = client.post(TOKEN_URL, data={
            "code": code, "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret, "redirect_uri": settings.gmail_redirect_uri,
            "grant_type": "authorization_code"})
    if response.status_code != 200:
        raise GmailConnectionRequired("Google did not accept the authorization; try connecting again")
    body = response.json()
    granted = set((body.get("scope") or "").split())
    if GMAIL_READONLY_SCOPE not in granted:
        raise GmailConnectionRequired("Gmail read-only access was not granted")
    refresh, access = body.get("refresh_token"), body.get("access_token")
    if not refresh or not access:
        raise GmailConnectionRequired("Google returned no offline access; remove Job Hunt OS from your Google account "
                                      "permissions and connect again")
    profile = GmailClient(access, user_id).profile()
    conn = connection_for(db, user_id) or GmailConnection(user_id=user_id)
    conn.refresh_token_enc = encrypt_secret(refresh)
    conn.email_address = (profile.get("emailAddress") or "")[:320]
    conn.scopes = " ".join(sorted(granted))[:500]
    conn.status, conn.last_error, conn.connected_at = CONNECTED, None, utcnow()
    db.add(conn)
    db.commit()
    return conn


def mark(db: Session, conn: GmailConnection, status: str, error: str | None) -> None:
    conn.status, conn.last_error = status, redact_text(error, 500) if error else None
    db.commit()


def access_token(db: Session, conn: GmailConnection) -> str:
    """A fresh access token for one sync, never stored. A dead grant marks the connection CONNECTION_REQUIRED."""
    settings = _require_config()
    refresh = decrypt_secret(conn.refresh_token_enc)
    if not refresh:
        mark(db, conn, CONNECTION_REQUIRED, "No usable Gmail authorization is stored; connect Gmail again")
        raise GmailConnectionRequired("Gmail is not connected")
    try:
        with _client("GOOGLE_OAUTH", "refresh_token", conn.user_id) as client:
            response = client.post(TOKEN_URL, data={
                "client_id": settings.google_oauth_client_id, "client_secret": settings.google_oauth_client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token"})
    except RateLimitedCooldown as exc:
        raise GmailRateLimited(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise GmailError(f"Google sign-in could not be reached: {type(exc).__name__}") from exc
    if response.status_code == 429:
        raise GmailRateLimited("Google is rate limiting token refreshes")
    if response.status_code >= 500:
        raise GmailError(f"Google sign-in failed with HTTP {response.status_code}")
    body = _json(response)
    if response.status_code != 200 or not body.get("access_token"):
        reason = "expired or revoked" if body.get("error") == "invalid_grant" else f"HTTP {response.status_code}"
        mark(db, conn, CONNECTION_REQUIRED, f"Gmail authorization {reason}; connect Gmail again")
        raise GmailConnectionRequired(f"Gmail authorization {reason}; connect Gmail again")
    granted = (body.get("scope") or conn.scopes or "").split()
    if granted and GMAIL_READONLY_SCOPE not in granted:
        mark(db, conn, CONNECTION_REQUIRED, "Gmail read-only access is no longer granted")
        raise GmailConnectionRequired("gmail.readonly not granted")
    return body["access_token"]


def disconnect(db: Session, conn: GmailConnection) -> None:
    """Forget the grant here, and ask Google to revoke it (best effort: the local copy is gone either way)."""
    refresh = decrypt_secret(conn.refresh_token_enc)
    conn.refresh_token_enc, conn.status, conn.last_error = None, REVOKED, None
    db.commit()
    if refresh:
        try:
            with _client("GOOGLE_OAUTH", "revoke", conn.user_id) as client:
                client.post(REVOKE_URL, data={"token": refresh})
        except httpx.HTTPError:
            pass


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ───────────── Gmail API (GET only) ─────────────

class GmailClient:
    """The Gmail reads one sync makes, with one access token held in memory. Every request is a GET."""

    def __init__(self, token: str, user_id: int | None):
        self.user_id = user_id
        self._http = _client("GMAIL", "gmail_read", user_id, headers={"Authorization": f"Bearer {token}"})
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None, operation: str, *,
             no_capture: bool = False) -> dict[str, Any]:
        retries = max(0, get_settings().gmail_max_retries)
        for attempt in range(retries + 1):
            self.requests += 1
            extensions: dict[str, Any] = {"operation": operation, "attempt": attempt + 1}
            if no_capture:  # bodies that must never be kept (a verification code), whatever capture says
                extensions[NO_CAPTURE] = True
            try:
                response = self._http.get(f"{GMAIL_API}{path}", params=params, extensions=extensions)
            except RateLimitedCooldown as exc:
                raise GmailRateLimited(str(exc)) from exc
            except httpx.HTTPError as exc:
                if attempt < retries:
                    time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
                    continue
                raise GmailError(f"Gmail could not be reached: {type(exc).__name__}") from exc
            if response.status_code == 200:
                return _json(response)
            if response.status_code == 204:
                # A partial response (`fields=`) with nothing in it: a search that matched no message.
                return {}
            if response.status_code == 429:
                raise GmailRateLimited("Gmail is rate limiting this account")
            if response.status_code == 401:
                raise GmailConnectionRequired("Gmail rejected the access token")
            if response.status_code == 403:
                reason = str(_json(response).get("error", {})).lower()
                if "ratelimit" in reason.replace(" ", "") or "quota" in reason:
                    raise GmailRateLimited("Gmail quota exceeded for this account")
                raise GmailConnectionRequired("Gmail access was refused (permissions changed?)")
            if response.status_code == 404:
                return {}
            if response.status_code == 400:
                raise GmailBadRequest(f"Gmail refused the {operation} request (HTTP 400)")
            if response.status_code >= 500 and attempt < retries:
                time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
                continue
            raise GmailError(f"Gmail answered HTTP {response.status_code}")
        raise GmailError("Gmail could not be read")

    def profile(self) -> dict[str, Any]:
        return self._get("/profile", None, "profile")

    def list_message_ids(self, query: str, limit: int) -> list[tuple[str, str]]:
        """(message id, thread id) of messages matching a Gmail search, newest first. No content is fetched."""
        found: list[tuple[str, str]] = []
        page: str | None = None
        while len(found) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(found)), "includeSpamTrash": "false",
                      "fields": "messages(id,threadId),nextPageToken"}
            if page:
                params["pageToken"] = page
            body = self._get("/messages", params, "messages_list")
            found += [(m["id"], m.get("threadId") or "") for m in body.get("messages") or [] if m.get("id")]
            page = body.get("nextPageToken")
            if not page:
                break
        return found[:limit]

    def thread_message_ids(self, thread_id: str) -> list[tuple[str, str]]:
        body = self._get(f"/threads/{thread_id}", {"format": "minimal", "fields": "messages(id,threadId)"},
                         "thread_get")
        return [(m["id"], m.get("threadId") or thread_id) for m in body.get("messages") or [] if m.get("id")]

    def get_metadata(self, message_id: str) -> dict[str, Any]:
        params = [("format", "metadata"), *(("metadataHeaders", h) for h in METADATA_HEADERS)]
        return self._get(f"/messages/{message_id}", params, "message_metadata")

    def get_full(self, message_id: str, *, no_capture: bool = False) -> dict[str, Any]:
        return self._get(f"/messages/{message_id}", {"format": "full"}, "message_full", no_capture=no_capture)


# ───────────── reading a message ─────────────

AUTOMATED_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|jobs-noreply|alerts?)\b", re.IGNORECASE)


@dataclass
class EmailMeta:
    message_id: str
    thread_id: str
    sender: str  # "Name <address>" as shown
    sender_name: str
    sender_email: str
    sender_domain: str
    subject: str
    received_at: datetime | None
    snippet: str
    automated: bool
    to: list[str] = field(default_factory=list)


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (message.get("payload") or {}).get("headers") or []}


def _received(message: dict[str, Any], headers: dict[str, str]) -> datetime | None:
    internal = message.get("internalDate")
    if internal and str(internal).isdigit():
        return datetime.fromtimestamp(int(internal) / 1000, UTC).replace(tzinfo=None)
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_meta(message: dict[str, Any]) -> EmailMeta:
    headers = _headers(message)
    name, address = (getaddresses([headers.get("from", "")]) or [("", "")])[0]
    address = address.strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    automated = bool(headers.get("list-unsubscribe")) or headers.get("auto-submitted", "no").lower() not in ("", "no") \
        or headers.get("precedence", "").lower() in ("bulk", "list", "junk") \
        or bool(AUTOMATED_LOCAL.match(address.split("@", 1)[0]))
    return EmailMeta(
        message_id=str(message.get("id") or ""), thread_id=str(message.get("threadId") or ""),
        sender=headers.get("from", "")[:500], sender_name=name.strip()[:200], sender_email=address[:320],
        sender_domain=domain[:255], subject=headers.get("subject", "")[:1000], received_at=_received(message, headers),
        snippet=html.unescape(message.get("snippet") or "")[:500], automated=automated,
        to=[a.lower() for _, a in getaddresses([headers.get("to", "")]) if a])


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stack, out = [payload], []
    while stack:
        part = stack.pop()
        out.append(part)
        stack.extend(reversed(part.get("parts") or []))
    return out


def _html_to_text(markup: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for quoted in soup.select("blockquote, .gmail_quote, #appendonsend"):
        quoted.decompose()
    for link in soup.find_all("a"):  # keep link targets: meeting and assessment links live there
        href = link.get("href") or ""
        if href.startswith("https://") and href not in link.get_text():
            link.append(f" ({href})")
    return soup.get_text("\n")


QUOTE_START = re.compile(r"^(On .{5,200} wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+|_{10,})\s*$", re.IGNORECASE | re.MULTILINE)


def body_text(message: dict[str, Any], limit: int) -> str:
    """The new text of a message: text/plain when there is one, else its HTML as text; quoted history removed."""
    parts = _parts(message.get("payload") or {})
    plain = [_decode((p.get("body") or {}).get("data")) for p in parts if p.get("mimeType") == "text/plain"]
    text = "\n".join(t for t in plain if t.strip())
    if not text.strip():
        markup = "\n".join(_decode((p.get("body") or {}).get("data")) for p in parts if p.get("mimeType") == "text/html")
        text = _html_to_text(markup) if markup.strip() else ""
    quoted = QUOTE_START.search(text)
    if quoted and quoted.start() > 40:
        text = text[:quoted.start()]
    lines = [line.rstrip() for line in text.splitlines() if not line.lstrip().startswith(">")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return redact_text(text, limit) or ""


def gmail_link(email_address: str | None, thread_id: str | None) -> str | None:
    """Open the thread in Gmail (the person's own mailbox). Carries ids only, never content."""
    if not thread_id or not re.fullmatch(r"[0-9a-fA-F]{6,32}", thread_id):
        return None
    account = f"?authuser={email_address}" if email_address and "@" in email_address else ""
    return f"https://mail.google.com/mail/u/0/{account}#all/{thread_id}"


def after_epoch(moment: datetime) -> int:
    return int(moment.replace(tzinfo=UTC).timestamp())


def sync_window_start(applied_at: datetime | None, last_success: datetime | None, now: datetime) -> datetime:
    """Where a search starts: the application date on the first sync (at most the initial lookback), then the last
    successful sync minus the overlap — never before the application itself."""
    settings = get_settings()
    floor = (applied_at or now) - timedelta(days=1)
    if last_success is None:
        return max(floor, now - timedelta(days=max(1, settings.gmail_initial_lookback_days)))
    return max(floor, last_success - timedelta(hours=max(0, settings.gmail_sync_overlap_hours)))
