"""Password hashing and encryption of the API keys users store.

Passwords use scrypt from the standard library with a random salt per password. API keys are encrypted
with Fernet (AES + HMAC) under a key derived from SECRET_KEY; without SECRET_KEY no key can be stored,
and changing SECRET_KEY makes previously stored keys unreadable (users re-enter them).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.settings import get_settings

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LENGTH = 2 ** 14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LENGTH)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        candidate = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                   dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


# Checked when the email is unknown, so a failed sign-in takes as long whether or not the account exists.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(24))


class SecretsUnavailable(RuntimeError):
    """SECRET_KEY is not configured, so secrets cannot be encrypted."""


def _fernet() -> Fernet:
    secret = get_settings().secret_key
    if not secret:
        raise SecretsUnavailable("The server has no SECRET_KEY, so API keys cannot be stored")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    """The stored secret, or None when nothing is stored or it cannot be decrypted with the current SECRET_KEY."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, SecretsUnavailable):
        return None
