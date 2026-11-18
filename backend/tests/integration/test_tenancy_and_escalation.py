"""Phase 2 gate: cross-tenant isolation, role escalation and audit integrity."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import BusinessRuleError, ForbiddenError
from app.core.tenancy import bind_tenant
from app.security.permissions import P
from app.security.principal import Principal
from app.services import user_service
from app.services.audit_service import AuditActor
from tests.integration.conftest import ApiClient, accept, invite, register, unique_email

pytestmark = pytest.mark.integration


# ---- Cross-tenant access through the API ---------------------------------------------------


def test_users_of_another_organisation_are_invisible(new_client: Callable[[], ApiClient]) -> None:
    org_a, org_b = new_client(), new_client()
    register(org_a, org="Org A")
    register(org_b, org="Org B")
    victim = invite(org_b, ["claims_adjuster"])["user"]

    listing = org_a.get("/users").json()
    assert victim["id"] not in {u["id"] for u in listing["items"]}
    for response in (
        org_a.get(f"/users/{victim['id']}"),
        org_a.patch(f"/users/{victim['id']}", json={"status": "disabled"}, headers={"If-Match": '"1"'}),
        org_a.put(f"/users/{victim['id']}/roles", json={"roles": ["auditor"]}),
        org_a.post(f"/users/{victim['id']}/reset-access"),
        org_a.get(f"/users/{victim['id']}/login-activity"),
    ):
        assert response.status_code == 404, response.text  # 404, not 403: no existence leak
    assert org_b.get(f"/users/{victim['id']}").json()["status"] == "invited"


def test_audit_log_is_tenant_scoped(new_client: Callable[[], ApiClient]) -> None:
    org_a, org_b = new_client(), new_client()
    me_a, me_b = register(org_a, org="Org A"), register(org_b, org="Org B")
    events = org_a.get("/audit-logs", params={"limit": 200}).json()["items"]
    actors = {e["actor_id"] for e in events}
    assert me_a["user"]["id"] in actors
    assert me_b["user"]["id"] not in actors
    assert org_a.get("/audit-logs", params={"entity_id": me_b["organization"]["id"]}).json()["items"] == []


def test_roles_are_per_organisation(new_client: Callable[[], ApiClient]) -> None:
    org_a, org_b = new_client(), new_client()
    register(org_a)
    register(org_b)
    ids_a = {r["id"] for r in org_a.get("/roles").json()}
    ids_b = {r["id"] for r in org_b.get("/roles").json()}
    assert len(ids_a) == 7 and ids_a.isdisjoint(ids_b)


# ---- Database-level isolation (defence in depth) ---------------------------------------------


async def test_rls_hides_other_tenants_even_without_where_clauses(
    new_client: Callable[[], ApiClient], app_engine: AsyncEngine
) -> None:
    org_a, org_b = new_client(), new_client()
    a, b = register(org_a), register(org_b)
    async with app_engine.connect() as conn:
        unbound = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
        assert unbound == 0  # no tenant bound -> no rows at all
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": a["organization"]["id"]})
        orgs = set((await conn.execute(text("SELECT organization_id::text FROM users"))).scalars())
        assert orgs == {a["organization"]["id"]}
        sessions = set(
            (await conn.execute(text("SELECT organization_id::text FROM user_sessions"))).scalars()
        )
        assert b["organization"]["id"] not in sessions


async def test_rls_rejects_writes_into_another_tenant(
    new_client: Callable[[], ApiClient], app_engine: AsyncEngine
) -> None:
    a, b = register(new_client()), register(new_client())
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": a["organization"]["id"]})
        with pytest.raises(DBAPIError, match="row-level security"):
            await conn.execute(
                text("INSERT INTO users (organization_id, email, display_name) VALUES (:o, :e, 'x')"),
                {"o": b["organization"]["id"], "e": unique_email()},
            )


async def test_composite_foreign_keys_block_cross_tenant_links(
    new_client: Callable[[], ApiClient], migrator_engine: AsyncEngine
) -> None:
    a, b = register(new_client()), register(new_client())
    async with migrator_engine.connect() as conn:  # schema owner: RLS bypassed, constraints still apply
        role_b = (
            await conn.execute(
                text("SELECT id FROM roles WHERE organization_id = :o LIMIT 1"),
                {"o": b["organization"]["id"]},
            )
        ).scalar_one()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text("INSERT INTO user_roles (organization_id, user_id, role_id) VALUES (:o, :u, :r)"),
                {"o": a["organization"]["id"], "u": a["user"]["id"], "r": role_b},
            )


# ---- Role escalation -----------------------------------------------------------------------


def test_non_admin_cannot_manage_users(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    manager = new_client()
    accept(manager, invite(admin, ["claims_manager"]))
    assert manager.get("/users").status_code == 403
    response = manager.post(
        "/users", json={"email": unique_email(), "display_name": "x", "roles": ["org_admin"]}
    )
    assert response.status_code == 403


def test_users_cannot_change_their_own_roles_or_status(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    me = register(admin)
    uid = me["user"]["id"]
    assert admin.put(f"/users/{uid}/roles", json={"roles": ["auditor"]}).status_code == 403
    etag = admin.get(f"/users/{uid}").headers["ETag"]
    assert (
        admin.patch(f"/users/{uid}", json={"status": "disabled"}, headers={"If-Match": etag}).status_code
        == 403
    )


async def test_cannot_grant_roles_beyond_own_permissions(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    me = register(admin)
    target = invite(admin, ["auditor"])["user"]
    org_id = uuid.UUID(me["organization"]["id"])
    # A delegated user administrator: may manage users but holds no other permission.
    delegate = Principal(
        user_id=uuid.uuid4(),
        organization_id=org_id,
        session_id=uuid.uuid4(),
        email="d@x.test",
        display_name="Delegate",
        role_keys=frozenset(),
        permissions=frozenset({P.USERS_MANAGE.value}),
        csrf_hash=b"",
    )
    db = Database.from_settings(settings)
    try:
        async with db.sessionmaker() as session:
            await bind_tenant(session, org_id)
            with pytest.raises(ForbiddenError, match="exceeds your own permissions"):
                await user_service.set_roles(
                    session,
                    delegate,
                    AuditActor("user", delegate.user_id),
                    uuid.UUID(target["id"]),
                    {"org_admin"},
                )
            with pytest.raises(ForbiddenError, match="auditor"):  # revoking is checked too
                await user_service.set_roles(
                    session,
                    delegate,
                    AuditActor("user", delegate.user_id),
                    uuid.UUID(target["id"]),
                    {"reviewer"},
                )
    finally:
        await db.dispose()


async def test_last_active_admin_cannot_be_removed(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    me = register(new_client())
    org_id, admin_id = uuid.UUID(me["organization"]["id"]), uuid.UUID(me["user"]["id"])
    other_admin = Principal(
        user_id=uuid.uuid4(),
        organization_id=org_id,
        session_id=uuid.uuid4(),
        email="o@x.test",
        display_name="Other",
        role_keys=frozenset({"org_admin"}),
        permissions=frozenset(p.value for p in P),
        csrf_hash=b"",
    )
    db = Database.from_settings(settings)
    try:
        async with db.sessionmaker() as session:
            await bind_tenant(session, org_id)
            with pytest.raises(BusinessRuleError, match="at least one active administrator"):
                await user_service.set_roles(
                    session, other_admin, AuditActor("user", other_admin.user_id), admin_id, {"auditor"}
                )
        async with db.sessionmaker() as session:
            await bind_tenant(session, org_id)
            with pytest.raises(BusinessRuleError, match="at least one active administrator"):
                await user_service.update_user(
                    session,
                    other_admin,
                    AuditActor("user", other_admin.user_id),
                    admin_id,
                    expected_version=1,
                    display_name=None,
                    status="disabled",
                )
    finally:
        await db.dispose()


# ---- User lifecycle ---------------------------------------------------------------------------


def test_invitation_disable_and_reset_flow(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    invitation = invite(admin, ["claims_adjuster"])
    user_id = invitation["user"]["id"]
    assert "#token=" in invitation["access_link"]["url"]

    member = new_client()
    me = accept(member, invitation)
    assert me["roles"] == ["claims_adjuster"]
    reused = new_client().post(
        "/auth/invitations/accept",
        json={
            "token": invitation["access_link"]["url"].split("#token=")[1],
            "password": "another long password",
        },
    )
    assert reused.status_code == 422  # one-time link

    stale = admin.patch(f"/users/{user_id}", json={"status": "disabled"}, headers={"If-Match": '"999"'})
    assert stale.status_code == 412
    assert admin.patch(f"/users/{user_id}", json={"status": "disabled"}).status_code == 428
    etag = admin.get(f"/users/{user_id}").headers["ETag"]
    assert (
        admin.patch(f"/users/{user_id}", json={"status": "disabled"}, headers={"If-Match": etag}).status_code
        == 200
    )
    assert member.get("/auth/me").status_code == 401  # sessions revoked on disable

    etag = admin.get(f"/users/{user_id}").headers["ETag"]
    assert (
        admin.patch(f"/users/{user_id}", json={"status": "active"}, headers={"If-Match": etag}).json()[
            "status"
        ]
        == "active"
    )
    link = admin.post(f"/users/{user_id}/reset-access").json()
    assert link["purpose"] == "reset"
    accept(new_client(), {"access_link": link}, password="a reset long password")


# ---- Audit integrity ---------------------------------------------------------------------------


async def test_audit_chain_verifies_and_detects_tampering(
    new_client: Callable[[], ApiClient], app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    me = register(admin)
    invite(admin, ["auditor"])
    assert admin.get("/audit-logs/verify").json() == {
        "valid": True,
        "events_checked": 3,
        "first_invalid_seq": None,
        "reason": None,
    }

    async with app_engine.connect() as conn:  # the runtime role cannot rewrite history
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": me["organization"]["id"]})
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text("UPDATE audit_events SET action = 'x'"))

    async with migrator_engine.begin() as conn:  # simulate a privileged attacker bypassing the trigger
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_append_only"))
        await conn.execute(
            text(
                'UPDATE audit_events SET after = \'{"email": "forged@x.test"}\' '
                "WHERE organization_id = :o AND seq = 2"
            ),
            {"o": me["organization"]["id"]},
        )
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_append_only"))
    result = admin.get("/audit-logs/verify").json()
    assert result["valid"] is False
    assert result["first_invalid_seq"] == 2
