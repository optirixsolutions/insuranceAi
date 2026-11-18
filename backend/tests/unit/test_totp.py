"""TOTP against the RFC 6238 appendix B test vectors (SHA-1, 8 digits) and the 6-digit app defaults."""

from __future__ import annotations

import pytest

from app.security import totp

RFC_KEY = b"12345678901234567890"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_sha1_vectors(timestamp: int, expected: str) -> None:
    assert totp.hotp(RFC_KEY, timestamp // 30, digits=8) == expected


def test_match_accepts_one_step_of_drift_and_rejects_others() -> None:
    secret = totp.new_secret()
    now = 1_800_000_000.0
    current = totp.step_at(now)
    assert totp.match(secret, totp.code_at(secret, now), now=now) == current
    assert totp.match(secret, totp.code_at(secret, now - 30), now=now) == current - 1
    assert totp.match(secret, totp.code_at(secret, now + 30), now=now) == current + 1
    assert totp.match(secret, totp.code_at(secret, now - 90), now=now) is None
    assert totp.match(secret, "12345", now=now) is None and totp.match(secret, "abcdef", now=now) is None
    code = totp.code_at(secret, now)
    assert totp.match(secret, f" {code[:3]} {code[3:]} ", now=now) == current  # spaces are allowed


def test_secret_and_provisioning_uri() -> None:
    secret = totp.new_secret()
    assert len(secret) == 32 and secret.isupper()
    uri = totp.provisioning_uri(secret, "reviewer@example.test")
    assert uri.startswith("otpauth://totp/ClaimOS%3Areviewer%40example.test?")
    assert f"secret={secret}" in uri and "issuer=ClaimOS" in uri and "period=30" in uri
