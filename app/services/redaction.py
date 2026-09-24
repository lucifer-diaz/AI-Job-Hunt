"""One place that decides what is safe to store about an external call.

Telemetry is written automatically by the instrumentation layer (app.services.external_api), so no caller is
trusted to sanitise anything: headers, query strings, payload extracts and error messages are cleaned here,
before they reach the database or the log file.

Nothing that could be a credential is ever stored: header values are replaced wholesale for known credential
headers, and secret-shaped strings are replaced anywhere they appear in free text.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[redacted]"
# Header names whose value is always a credential. Matched case-insensitively, in full.
SECRET_HEADERS = frozenset({
    "authorization", "proxy-authorization", "www-authenticate", "proxy-authenticate",
    "cookie", "set-cookie", "x-api-key", "api-key", "apikey", "x-auth-token", "auth-token",
    "x-amz-security-token", "x-goog-api-key", "x-anthropic-api-key", "x-openai-api-key",
    "openai-organization", "x-apify-token", "x-access-token", "x-session-token", "x-csrf-token",
})
# Query-string and body field names that carry a credential or a person's identity.
SECRET_FIELDS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey", "key", "secret",
    "client_secret", "password", "passwd", "pwd", "authorization", "auth", "session", "sig", "signature",
    "code", "email", "phone",
})
# Secret-shaped strings, redacted wherever they appear in free text (error messages, URLs, payload extracts).
SECRET_PATTERNS = [
    re.compile(r"sk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"apify_api_[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:x-)?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"),
    re.compile(r"(?i)((?:access_|refresh_|id_)?token[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{8,}"),
    re.compile(r"(?i)(password[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
]
MAX_ERROR_MESSAGE = 1000


def redact_text(value: str | None, limit: int = MAX_ERROR_MESSAGE) -> str | None:
    """Replace anything secret-shaped in free text, then cap its length."""
    if value is None:
        return None
    clean = value
    for pattern in SECRET_PATTERNS:
        clean = pattern.sub(lambda m: (m.group(1) if m.groups() and m.group(1) else "") + REDACTED, clean)
    return clean[:limit]


def redact_headers(headers: Any) -> dict[str, str]:
    """Header names with safe values: credential headers keep only their name."""
    items = headers.items() if hasattr(headers, "items") else (headers or [])
    return {str(name): REDACTED if str(name).lower() in SECRET_HEADERS else (redact_text(str(value), 200) or "")
            for name, value in items}


def redact_params(params: Any) -> dict[str, str]:
    pairs = params.items() if hasattr(params, "items") else (params or [])
    return {str(k): REDACTED if str(k).lower() in SECRET_FIELDS else (redact_text(str(v), 200) or "") for k, v in pairs}


def redact_url(url: str) -> str:
    """The URL without any secret query values — what is safe to show in a log line."""
    parts = urlsplit(str(url))
    query = urlencode([(k, REDACTED if k.lower() in SECRET_FIELDS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)])
    return redact_text(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")), 2000) or ""


def redact_value(value: Any, depth: int = 0) -> Any:
    """A JSON-ish structure with secret fields removed, for the small metadata dictionaries we keep."""
    if depth > 6:
        return REDACTED
    if isinstance(value, dict):
        return {str(k): REDACTED if str(k).lower() in SECRET_FIELDS else redact_value(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return redact_text(value, 500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value), 200)
