"""Two-factor authentication against the real stack: enrolment, sign-in challenge, replay and brute-force
protection, recovery codes, organisation policy and admin reset (Phase 13)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.security import totp
from tests.integration.conftest import PASSWORD, ApiClient, accept, invite, register, unique_email

pytestmark = pytest.mark.integration


def _enrol(client: ApiClient) -> tuple[str, str, list[str]]:
    """Turn MFA on; returns (secret, the code used to confirm, recovery codes)."""
    setup = client.post("/auth/mfa/setup")
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/ClaimOS")
    code = totp.code_at(secret, time.time())
    confirmed = client.post("/auth/mfa/confirm", json={"code": code})
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    return secret, code, codes


def _login(client: ApiClient, email: str) -> dict[str, Any]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _next_code(secret: str) -> str:
    """A code for the next time step: valid (one step of drift) and later than any code used so far."""
    return totp.code_at(secret, time.time() + totp.STEP_SECONDS)


def _wrong_code(secret: str) -> str:
    valid = {totp.code_at(secret, time.time() + d) for d in (-60, -30, 0, 30, 60)}
    return next(c for c in ("000000", "111111", "222222", "333333", "444444", "555555") if c not in valid)


async def test_enrolment_and_two_step_sign_in(
    new_client: Callable[[], ApiClient], migrator_engine: AsyncEngine
) -> None:
    email = unique_email("mfa")
    owner = new_client()
    me = register(owner, email=email)
    assert owner.get("/auth/mfa").json()["enabled"] is False
    assert owner.post("/auth/mfa/confirm", json={"code": "123456"}).status_code == 404  # no set-up yet
    draft = owner.post("/auth/mfa/setup").json()["secret"]
    assert owner.post("/auth/mfa/confirm", json={"code": _wrong_code(draft)}).status_code == 422

    secret, confirm_code, recovery = _enrol(owner)  # a second set-up replaces the unconfirmed secret
    assert secret != draft
    status = owner.get("/auth/mfa").json()
    assert status["enabled"] is True and status["recovery_codes_left"] == 10
    assert owner.post("/auth/mfa/setup").status_code == 409

    async with migrator_engine.connect() as conn:
        stored = await conn.scalar(
            text("SELECT secret_enc FROM user_mfa WHERE user_id = :u"), {"u": me["user"]["id"]}
        )
    assert stored and secret not in stored  # encrypted at rest

    # Password alone gives a challenge, not a session.
    browser = new_client()
    first = _login(browser, email)
    assert first["mfa_required"] is True and first["challenge"].startswith(me["organization"]["id"])
    assert browser.get("/auth/me").status_code == 401

    replay = browser.post("/auth/mfa/verify", json={"challenge": first["challenge"], "code": confirm_code})
    assert replay.status_code == 401  # the confirmation code was already used
    ok = browser.post("/auth/mfa/verify", json={"challenge": first["challenge"], "code": _next_code(secret)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["email"] == email
    assert browser.get("/auth/me").status_code == 200
    reused = browser.post("/auth/mfa/verify", json={"challenge": first["challenge"], "code": recovery[1]})
    assert reused.status_code == 401  # a challenge works once

    # A recovery code signs in once (case and dash insensitive).
    second = new_client()
    challenge = _login(second, email)["challenge"]
    recovered = second.post("/auth/mfa/verify", json={"challenge": challenge, "code": recovery[0]})
    assert recovered.status_code == 200
    third = new_client()
    challenge = _login(third, email)["challenge"]
    again = third.post("/auth/mfa/verify", json={"challenge": challenge, "code": recovery[0].upper()})
    assert again.status_code == 401
    assert owner.get("/auth/mfa").json()["recovery_codes_left"] == 9

    async with migrator_engine.connect() as conn:
        actions = set(
            (
                await conn.scalars(
                    text("SELECT action FROM audit_events WHERE organization_id = :o"),
                    {"o": me["organization"]["id"]},
                )
            ).all()
        )
    assert {"auth.mfa_enabled", "auth.mfa_recovery_code_used", "auth.login_succeeded"} <= actions


async def test_challenge_limits_attempts_and_expires(
    new_client: Callable[[], ApiClient], migrator_engine: AsyncEngine
) -> None:
    email = unique_email("mfa")
    owner = new_client()
    me = register(owner, email=email)
    secret, _, _ = _enrol(owner)

    client = new_client()
    challenge = _login(client, email)["challenge"]
    for _ in range(5):
        bad = client.post("/auth/mfa/verify", json={"challenge": challenge, "code": _wrong_code(secret)})
        assert bad.status_code == 401
    blocked = client.post("/auth/mfa/verify", json={"challenge": challenge, "code": _next_code(secret)})
    assert blocked.status_code == 401 and "Too many" in blocked.json()["detail"]

    challenge = _login(client, email)["challenge"]
    async with migrator_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mfa_challenges SET expires_at = now() - interval '1 second' WHERE user_id = :u"),
            {"u": me["user"]["id"]},
        )
    expired = client.post("/auth/mfa/verify", json={"challenge": challenge, "code": _next_code(secret)})
    assert expired.status_code == 401 and "expired" in expired.json()["detail"]

    forged = client.post("/auth/mfa/verify", json={"challenge": "not-a-uuid." + "x" * 43, "code": "123456"})
    assert forged.status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_disable_needs_password_and_code(new_client: Callable[[], ApiClient]) -> None:
    email = unique_email("mfa")
    owner = new_client()
    register(owner, email=email)
    secret, _, _ = _enrol(owner)
    wrong_pw = owner.post(
        "/auth/mfa/disable", json={"password": "wrong password!", "code": _next_code(secret)}
    )
    assert wrong_pw.status_code == 422
    wrong_code = owner.post("/auth/mfa/disable", json={"password": PASSWORD, "code": _wrong_code(secret)})
    assert wrong_code.status_code == 422
    off = owner.post("/auth/mfa/disable", json={"password": PASSWORD, "code": _next_code(secret)})
    assert off.status_code == 204, off.text
    assert owner.get("/auth/mfa").json()["enabled"] is False
    assert "user" in _login(new_client(), email)  # password-only sign-in again


def test_organisation_policy_and_admin_reset(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    admin_me = register(admin)
    adjuster = new_client()
    adjuster_me = accept(adjuster, invite(admin, ["claims_adjuster"]))
    assert adjuster.get("/claims").status_code == 200

    assert adjuster.put("/organization/mfa-policy", json={"required_roles": []}).status_code == 403
    policy = admin.put("/organization/mfa-policy", json={"required_roles": ["claims_adjuster"]})
    assert policy.status_code == 200, policy.text
    assert policy.json() == {"required_roles": ["claims_adjuster"]}
    assert admin.get("/organization/mfa-policy").json() == {"required_roles": ["claims_adjuster"]}

    blocked = adjuster.get("/claims")
    assert blocked.status_code == 403 and "two-factor" in blocked.json()["detail"]
    assert adjuster.get("/auth/mfa").json()["required"] is True
    secret, _, _ = _enrol(adjuster)
    assert adjuster.get("/claims").status_code == 200
    refused = adjuster.post("/auth/mfa/disable", json={"password": PASSWORD, "code": _next_code(secret)})
    assert refused.status_code == 403

    assert admin.post(f"/users/{admin_me['user']['id']}/mfa/reset").status_code == 403
    assert admin.post(f"/users/{adjuster_me['user']['id']}/mfa/reset").status_code == 204
    assert adjuster.get("/auth/mfa").json()["enabled"] is False
    assert adjuster.get("/claims").status_code == 403  # must enrol again
    assert admin.get("/claims").status_code == 200  # the admin role is not covered by the policy
