from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from app.api.deps import parse_if_match
from app.api.v1.audit import _decode_cursor, _encode_cursor
from app.core.config import Settings
from app.core.errors import BusinessRuleError, PreconditionRequiredError
from app.security.passwords import PasswordPolicyError, hash_password, validate_password, verify_password
from app.security.permissions import ADMIN_ROLE_KEY, PERMISSIONS, SYSTEM_ROLES, P
from app.security.principal import Principal
from app.security.tokens import email_fingerprint, hash_token, new_token, tokens_match
from app.services.audit_service import GENESIS_HASH, compute_hash


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "at least 12"),
        ("x" * 129, "at most 128"),
        (" leading space pw", "whitespace"),
        ("person@example.test", "email"),
    ],
)
def test_password_policy_rejects(password: str, message: str) -> None:
    with pytest.raises(PasswordPolicyError, match=message):
        validate_password(password, email="person@example.test")


def test_password_hash_round_trip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong horse battery staple")
    assert not verify_password(None, "anything at all here")
    assert not verify_password("not-a-hash", "anything at all here")


def test_tokens_are_random_and_compared_by_digest() -> None:
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert tokens_match(a, hash_token(a))
    assert not tokens_match(b, hash_token(a))
    assert email_fingerprint(" User@Example.TEST ") == email_fingerprint("user@example.test")


def test_role_catalogue_is_consistent() -> None:
    keys = [r.key for r in SYSTEM_ROLES]
    assert len(keys) == len(set(keys)) == 7
    admin = next(r for r in SYSTEM_ROLES if r.key == ADMIN_ROLE_KEY)
    assert admin.permissions == frozenset(P)
    assert set(PERMISSIONS) == set(P)
    auditor = next(r for r in SYSTEM_ROLES if r.key == "auditor")
    assert auditor.permissions == {P.CLAIMS_READ, P.DOCUMENTS_READ, P.AUDIT_READ}
    for role in SYSTEM_ROLES:
        if role.key != ADMIN_ROLE_KEY:
            assert P.USERS_MANAGE not in role.permissions, role.key


def test_principal_permission_subset_check() -> None:
    principal = Principal(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "a@b.c",
        "A",
        frozenset(),
        frozenset({"claims.read", "audit.read"}),
        b"",
    )
    assert principal.holds_all(frozenset({"claims.read"}))
    assert not principal.holds_all(frozenset({"claims.read", "users.manage"}))


def test_audit_hash_is_deterministic_and_chained() -> None:
    payload = {"seq": 1, "action": "user.invited", "after": {"b": 2, "a": 1}}
    reordered = {"after": {"a": 1, "b": 2}, "action": "user.invited", "seq": 1}
    first = compute_hash(GENESIS_HASH, payload)
    assert first == compute_hash(GENESIS_HASH, reordered)
    assert compute_hash(first, payload) != first
    assert compute_hash(GENESIS_HASH, {**payload, "seq": 2}) != first


@pytest.mark.parametrize(("value", "expected"), [('"3"', 3), ('W/"7"', 7), ("12", 12)])
def test_if_match_parsing(value: str, expected: int) -> None:
    assert parse_if_match(value) == expected


@pytest.mark.parametrize("value", [None, '"abc"', ""])
def test_if_match_required(value: str | None) -> None:
    with pytest.raises(PreconditionRequiredError):
        parse_if_match(value)


def test_audit_cursor_round_trip() -> None:
    assert _decode_cursor(_encode_cursor(42)) == 42
    with pytest.raises(BusinessRuleError):
        _decode_cursor("not-a-cursor")


def test_allowed_origin_normalises_default_ports(make_settings: Callable[..., Settings]) -> None:
    assert make_settings(app_base_url="http://localhost:8080").allowed_origin == "http://localhost:8080"
    assert (
        make_settings(app_base_url="https://claims.example.com/").allowed_origin
        == "https://claims.example.com"
    )
    assert make_settings().self_registration_allowed is True
