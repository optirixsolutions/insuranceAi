"""Opaque random tokens. Only SHA-256 digests are stored server-side."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token() -> str:
    """256-bit URL-safe random token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def tokens_match(token: str, expected_hash: bytes) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)


def email_fingerprint(email: str) -> bytes:
    """Stable digest used for throttling/login records without storing raw emails."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).digest()
