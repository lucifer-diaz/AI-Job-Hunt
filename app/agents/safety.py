"""What may never leave a graph run: one check shared by the checkpointer, thread inputs and the LangSmith export.

`problem(value)` returns why a value must not be stored or exported, or None. It refuses credential-named fields
(by name, whatever the value) and secret-shaped strings anywhere (app.services.redaction.SECRET_PATTERNS). It refuses
rather than redacts: a graph that tries to checkpoint a token has a bug, and the run should stop, not continue with a
quietly altered state.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.redaction import SECRET_FIELD_RE, SECRET_PATTERNS

# Field names that are authentication material whatever their value. Generic words the redaction list also covers
# ("key", "code", "session") are left to the value patterns, so LangGraph's own bookkeeping is not refused.
DENIED_KEYS = frozenset({
    "authorization", "proxy-authorization", "cookie", "cookies", "set-cookie", "password", "passwd", "passphrase",
    "otp", "one_time_code", "totp", "mfa_code", "verification_code", "api_key", "apikey", "x-api-key",
    "access_token", "refresh_token", "id_token", "client_secret", "storage_state", "browser_profile",
    "user_data_dir", "session_cookie", "session_token", "credentials", "private_key",
})
MAX_DEPTH = 24


def denied_key(name: Any) -> bool:
    key = str(name).lower()
    return key in DENIED_KEYS or bool(SECRET_FIELD_RE.search(key))


def secret_shaped(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def problem(value: Any, depth: int = 0) -> str | None:
    """Why `value` must not be stored or exported (a field name or 'secret-shaped text'), or None."""
    if depth > MAX_DEPTH:
        return "nested too deeply"
    if isinstance(value, str):
        return "secret-shaped text" if secret_shaped(value) else None
    if isinstance(value, bytes):
        return "secret-shaped text" if secret_shaped(value.decode("utf-8", "ignore")) else None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and denied_key(key) and not isinstance(item, (bool, int, float, type(None))):
                return f"credential field '{_short(key)}'"
            found = problem(key, depth + 1) or problem(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found = problem(item, depth + 1)
            if found:
                return found
        return None
    # Another object (a LangGraph Interrupt, Send, a pydantic model): look at its fields.
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict):
        return problem(fields, depth + 1)
    if hasattr(value, "_asdict"):
        return problem(value._asdict(), depth + 1)
    return None


_SAFE_NAME = re.compile(r"[^a-z0-9_\-]")


def _short(key: str) -> str:
    return _SAFE_NAME.sub("", key.lower())[:40]
