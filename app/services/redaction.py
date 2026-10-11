"""One place that decides what is safe to store about an external call.

Telemetry is written automatically by the instrumentation layer (app.services.external_api), so no caller is
trusted to sanitise anything: headers, query strings, payload extracts and error messages are cleaned here,
before they reach the database or the log file.

Nothing that could be a credential is ever stored: header values are replaced wholesale for known credential
headers, and secret-shaped strings are replaced anywhere they appear in free text.

Field names are matched by shape as well as by list (`is_secret_field`): user_api_key, user_api_key_hash,
openai_api_key, database_url or x-custom-api-key are all credentials, while token *counts* such as input_tokens or
max_tokens are not. Every writer of telemetry, logs, debug text and Monitoring payloads goes through this module:
external_api (request logs), llm_telemetry (LLM logs and LLM_PROMPT_DEBUG text), system_log and litellm_client.
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
    # LiteLLM and provider credentials as they appear in configuration (app.services.ai_model_config).
    "master_key", "litellm_master_key", "litellm_api_key", "api_key_enc", "aws_secret_access_key",
    "aws_session_token", "vertex_credentials", "azure_ad_token", "salt_key", "litellm_salt_key",
})
# Field names that are credentials by shape: a credential word as a whole segment of the name (split on _ - .).
SECRET_FIELD_RE = re.compile(
    r"(?i)(?:^|[_\-.])(?:api[_\-]?key|secret|password|passwd|passphrase|authorization|cookie|credentials?|"
    r"private[_\-]?key|(?:access|refresh|id|auth|session|bearer|csrf)[_\-]?token|token|database[_\-]?url|"
    r"db[_\-]?url|dsn|connection[_\-]?string|master[_\-]?key|salt[_\-]?key)(?:$|[_\-.])")
_SECRET_NAME = (r"[a-z0-9_\-]*(?:api[_\-]?key|secret|password|passwd|authorization|cookie|credential|database[_\-]?url|"
                r"access[_\-]?token|refresh[_\-]?token|auth[_\-]?token|session[_\-]?token|private[_\-]?key)[a-z0-9_\-]*")
# Secret-shaped strings, redacted wherever they appear in free text (error messages, URLs, payload extracts).
SECRET_PATTERNS = [
    # A password inside a connection string or URL: scheme://user:PASSWORD@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+(?=@)"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWTs
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"sk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    # Google OAuth: access tokens, refresh tokens and client secrets (the Gmail connection, app.services.gmail).
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{10,}"),
    re.compile(r"(?<![\w/])1//[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bGOCSPX-[0-9A-Za-z_\-]{10,}"),
    re.compile(r"apify_api_[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:x-)?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"),
    re.compile(r"(?i)((?:access_|refresh_|id_)?token[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{8,}"),
    re.compile(r"(?i)(password[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    # Last, so the specific shapes above (a bearer token) are replaced whole first: any other credential-named
    # field in JSON or key=value text ("user_api_key": "…", database_url=…, x-foo-secret: …).
    re.compile(r"(?i)([\"']?" + _SECRET_NAME + r"[\"']?\s*[:=]\s*[\"']?)(?=[^\s\"',}]*[A-Za-z0-9])[^\s\"',}]{4,}"),
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


def is_secret_field(name: Any) -> bool:
    """Whether a header, query or JSON field name holds a credential (by list or by shape)."""
    key = str(name).lower()
    return key in SECRET_FIELDS or key in SECRET_HEADERS or bool(SECRET_FIELD_RE.search(key))


def _secret_entry(name: Any, value: Any) -> bool:
    """A listed credential name is always redacted. A name matched only by its shape (no_token, retry_token_count)
    is redacted when its value could hold a credential: a count or a flag cannot."""
    key = str(name).lower()
    if key in SECRET_FIELDS or key in SECRET_HEADERS:
        return True
    return bool(SECRET_FIELD_RE.search(key)) and not isinstance(value, (bool, int, float, type(None)))


def redact_headers(headers: Any) -> dict[str, str]:
    """Header names with safe values: credential headers keep only their name."""
    items = headers.items() if hasattr(headers, "items") else (headers or [])
    return {str(name): REDACTED if is_secret_field(name) else (redact_text(str(value), 200) or "")
            for name, value in items}


def redact_body(raw: bytes | str | None, limit: int) -> str | None:
    """A request or response body fit to keep for inspection: JSON and form fields with a credential name are
    replaced, then anything secret-shaped anywhere, then the text is capped. Never raises."""
    if raw is None or raw == b"" or raw == "":
        return None
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        import json

        def walk(value: Any, depth: int = 0) -> Any:
            if depth > 12:
                return value
            if isinstance(value, dict):
                return {k: REDACTED if _secret_entry(k, v) else walk(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v, depth + 1) for v in value]
            return value

        stripped = text.lstrip()
        if stripped[:1] in ("{", "["):
            text = json.dumps(walk(json.loads(text)), ensure_ascii=False, indent=2)
        elif "=" in text and "\n" not in text.strip() and " " not in text.strip():
            from urllib.parse import parse_qsl, urlencode

            pairs = parse_qsl(text, keep_blank_values=True)
            if pairs:
                # Field by field: a whole-text pass would read "client_secret=…&q=…" as one secret to the end.
                return "&".join(f"{urlencode([(k, '')])[:-1]}={REDACTED if _secret_entry(k, v) else redact_text(v, limit)}"
                                for k, v in pairs)[:limit]
    except (ValueError, TypeError):
        pass  # not JSON or a form after all: the pattern pass below still applies
    return redact_text(text, limit)


def redact_params(params: Any) -> dict[str, str]:
    pairs = params.items() if hasattr(params, "items") else (params or [])
    return {str(k): REDACTED if is_secret_field(k) else (redact_text(str(v), 200) or "") for k, v in pairs}


def redact_url(url: str) -> str:
    """The URL without any secret query values or embedded password — what is safe to show in a log line."""
    parts = urlsplit(str(url))
    query = urlencode([(k, REDACTED if is_secret_field(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)])
    return redact_text(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")), 2000) or ""


def redact_value(value: Any, depth: int = 0) -> Any:
    """A JSON-ish structure with secret fields removed, for the small metadata dictionaries we keep."""
    if depth > 6:
        return REDACTED
    if isinstance(value, dict):
        return {str(k): REDACTED if _secret_entry(k, v) else redact_value(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return redact_text(value, 500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value), 200)
