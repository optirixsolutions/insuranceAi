"""Time-based one-time passwords (RFC 6238, HOTP per RFC 4226), standard library only.

Secrets are 20 random bytes shown to the user as base32 (what authenticator apps expect). Codes are
6 digits over 30-second steps with HMAC-SHA1, the defaults every common authenticator app uses.
``match`` accepts the current step and one step either side (clock drift) and returns the step that
matched, so the caller can refuse any step at or before the last accepted one (no replays).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
STEP_SECONDS = 30
DRIFT_STEPS = 1


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest: str = "sha1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, digest)).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def step_at(timestamp: float) -> int:
    return int(timestamp // STEP_SECONDS)


def code_at(secret: str, timestamp: float) -> str:
    return hotp(_key(secret), step_at(timestamp))


def match(secret: str, code: str, *, now: float | None = None) -> int | None:
    """The time step the code belongs to (current +/- drift), or None."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    key = _key(secret)
    current = step_at(time.time() if now is None else now)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "ClaimOS") -> str:
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP_SECONDS})
    return f"otpauth://totp/{label}?{query}"
