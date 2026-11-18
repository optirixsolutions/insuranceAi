"""Authentication, sessions, CSRF and brute-force protection against real infrastructure."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.security.permissions import PERMISSIONS
from tests.integration.conftest import PASSWORD, ApiClient, register, unique_email

pytestmark = pytest.mark.integration


def test_register_creates_admin_session(new_client: Callable[[], ApiClient]) -> None:
    client = new_client()
    email = unique_email("founder")
    me = register(client, email=email)
    assert me["user"]["email"] == email
    assert me["roles"] == ["org_admin"]
    assert set(me["permissions"]) == {p.value for p in PERMISSIONS}

    session_cookie = next(c for c in client.http.cookies.jar if c.name == "__Host-claimos_session")
    assert session_cookie.secure
    assert session_cookie.has_nonstandard_attr("HttpOnly")
    assert client.get("/auth/me").json()["user"]["email"] == email


def test_login_failures_are_indistinguishable(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email()
    register(new_client(), email=email)
    client = new_client()
    wrong_password = client.post("/auth/login", json={"email": email, "password": "not the password!"})
    unknown_user = client.post("/auth/login", json={"email": unique_email("nobody"), "password": PASSWORD})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"] == "Invalid email or password"


def test_login_email_is_case_insensitive(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email()
    register(new_client(), email=email)
    response = new_client().post("/auth/login", json={"email": email.upper(), "password": PASSWORD})
    assert response.status_code == 200


def test_logout_revokes_session(new_client: Callable[[], ApiClient]) -> None:
    client = new_client()
    register(client)
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_unauthenticated_requests_are_rejected(new_client: Callable[[], ApiClient]) -> None:
    client = new_client()
    for path in ("/auth/me", "/users", "/audit-logs", "/organization", "/roles"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "UNAUTHENTICATED"


def test_csrf_token_is_required_for_state_changes(new_client: Callable[[], ApiClient]) -> None:
    client = new_client()
    register(client)
    missing = client.http.post("/api/v1/auth/logout")
    forged = client.http.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "forged-token-value"})
    assert missing.status_code == forged.status_code == 403
    assert missing.json()["code"] == "CSRF_FAILED"
    assert client.get("/auth/me").status_code == 200  # session untouched


def test_cross_origin_writes_are_rejected(new_client: Callable[[], ApiClient]) -> None:
    client = new_client()
    register(client)
    response = client.post("/auth/logout", headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_REJECTED"
    cross_site = client.post("/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403


def test_account_lockout_after_repeated_failures(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email("target")
    register(new_client(), email=email)
    attacker = new_client()
    for _ in range(5):
        assert (
            attacker.post("/auth/login", json={"email": email, "password": "wrong-password-xx"}).status_code
            == 401
        )
    locked = attacker.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0


async def test_idle_session_expires(
    new_client: Callable[[], ApiClient], migrator_engine: AsyncEngine
) -> None:
    client = new_client()
    me = register(client)
    async with migrator_engine.begin() as conn:
        await conn.execute(
            text("UPDATE user_sessions SET idle_expires_at = now() - interval '1 minute' WHERE user_id = :u"),
            {"u": me["user"]["id"]},
        )
    response = client.get("/auth/me")
    assert response.status_code == 401
    async with migrator_engine.connect() as conn:
        reason = await conn.scalar(
            text("SELECT revoke_reason FROM user_sessions WHERE user_id = :u"), {"u": me["user"]["id"]}
        )
    assert reason == "expired"


def test_password_change_signs_out_other_sessions(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email()
    first = new_client()
    register(first, email=email)
    second = new_client()
    assert second.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200

    new_password = "an entirely new passphrase"
    weak = first.post("/auth/password/change", json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 422
    changed = first.post(
        "/auth/password/change", json={"current_password": PASSWORD, "new_password": new_password}
    )
    assert changed.status_code == 204
    assert first.get("/auth/me").status_code == 200
    assert second.get("/auth/me").status_code == 401
    relogin = new_client().post("/auth/login", json={"email": email, "password": new_password})
    assert relogin.status_code == 200


def test_duplicate_registration_is_generic(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email()
    register(new_client(), email=email)
    response = new_client().post(
        "/auth/register",
        json={"organization_name": "Copycat", "display_name": "X", "email": email, "password": PASSWORD},
    )
    assert response.status_code == 409
    assert email not in response.text


def test_session_listing_and_revocation(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email()
    first = new_client()
    register(first, email=email)
    second = new_client()
    second.post("/auth/login", json={"email": email, "password": PASSWORD})
    sessions = first.get("/auth/sessions").json()
    assert len(sessions) == 2
    other = next(s for s in sessions if not s["current"])
    assert first.delete(f"/auth/sessions/{other['id']}").status_code == 204
    assert second.get("/auth/me").status_code == 401


async def test_permission_catalogue_matches_code(migrator_engine: AsyncEngine) -> None:
    async with migrator_engine.connect() as conn:
        keys = set((await conn.execute(text("SELECT key FROM permissions"))).scalars())
    assert keys == {p.value for p in PERMISSIONS}
