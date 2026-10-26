"""Password hashing, token generation and secret encryption.

Two different tools for two different jobs:
  * Passwords are low-entropy human input -> slow, salted KDF (scrypt).
  * API keys / session tokens are 256-bit random values -> a fast SHA-256 is
    sufficient (brute force is infeasible) and keeps per-request auth cheap.
  * Upstream credentials must be *recoverable* (we have to send them) -> they
    are encrypted at rest with Fernet (AES-128-CBC + HMAC), keys from a Secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
                                      base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token(prefix: str) -> str:
    """e.g. gw_<43 url-safe chars> (256 bits of entropy)."""
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DecryptionError(Exception):
    pass


class Crypto:
    """Encrypt/decrypt small JSON blobs. The first key encrypts; all keys decrypt,
    which allows zero-downtime key rotation (prepend a new key, re-save later)."""

    def __init__(self, keys: tuple[str, ...]):
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])

    def encrypt_json(self, value: dict) -> str:
        return self._fernet.encrypt(json.dumps(value).encode()).decode()

    def decrypt_json(self, blob: str) -> dict:
        try:
            return json.loads(self._fernet.decrypt(blob.encode()))
        except (InvalidToken, ValueError) as exc:
            raise DecryptionError("stored credential cannot be decrypted with the configured keys") from exc
