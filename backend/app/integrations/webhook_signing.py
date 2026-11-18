"""Webhook signatures (Phase 11). Receivers can copy ``verify`` as is.

Header: ``X-ClaimOS-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>``.
The timestamp is signed with the body, so a captured request cannot be replayed after the tolerance
window, and the receiver must compare in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

HEADER = "X-ClaimOS-Signature"
TOLERANCE_SECONDS = 300


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(
    secret: str, header: str, body: bytes, *, now: int | None = None, tolerance: int = TOLERANCE_SECONDS
) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((int(time.time()) if now is None else now) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)
