"""Password hashing (argon2id) and password policy."""

from __future__ import annotations

import contextlib

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 128

# OWASP-recommended argon2id parameters (64 MiB, 3 iterations, 4 lanes).
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
# Verified when the account does not exist, so response time does not reveal valid emails.
_DUMMY_HASH = _hasher.hash("claimos-timing-equaliser-not-a-password")


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, *, email: str | None = None) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_LENGTH} characters")
    if password.strip() != password or not password.strip():
        raise PasswordPolicyError("Password must not start or end with whitespace")
    if email and password.lower() == email.lower():
        raise PasswordPolicyError("Password must not be the email address")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if password_hash is None:
        _safe_dummy(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _safe_dummy(password: str) -> None:
    with contextlib.suppress(VerificationError):
        _hasher.verify(_DUMMY_HASH, password)


def burn_verification_time(password: str) -> None:
    """Spend the same time as a real verification for unknown accounts."""
    _safe_dummy(password)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
