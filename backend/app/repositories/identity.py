"""Tenant-scoped data access for identity tables.

All queries run on a session bound with ``bind_tenant``; row-level security restricts
them to that organisation. Explicit ``organization_id`` filters are kept as well, so a
missing binding fails closed (RLS returns nothing) and code reads clearly.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import Role, RolePermission, User, UserRole, UserSession


async def find_user_for_login(db: AsyncSession, email: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    row = (
        await db.execute(
            text("SELECT user_id, organization_id FROM claimos_auth_find_user(:e)"), {"e": email}
        )
    ).first()
    return (row[0], row[1]) if row else None


async def find_session(db: AsyncSession, token_hash: bytes) -> tuple[uuid.UUID, uuid.UUID] | None:
    row = (
        await db.execute(
            text("SELECT session_id, organization_id FROM claimos_auth_find_session(:h)"), {"h": token_hash}
        )
    ).first()
    return (row[0], row[1]) if row else None


async def find_access_token(db: AsyncSession, token_hash: bytes) -> tuple[uuid.UUID, uuid.UUID] | None:
    row = (
        await db.execute(
            text("SELECT user_id, organization_id FROM claimos_auth_find_access_token(:h)"), {"h": token_hash}
        )
    ).first()
    return (row[0], row[1]) if row else None


async def get_user(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> User | None:
    return await db.scalar(
        select(User).where(User.organization_id == org_id, User.id == user_id, User.deleted_at.is_(None))
    )


async def roles_by_key(db: AsyncSession, org_id: uuid.UUID) -> dict[str, Role]:
    roles = (await db.scalars(select(Role).where(Role.organization_id == org_id))).all()
    return {r.key: r for r in roles}


async def role_permissions(db: AsyncSession, org_id: uuid.UUID) -> dict[uuid.UUID, frozenset[str]]:
    rows = (
        await db.execute(
            select(RolePermission.role_id, RolePermission.permission_key).where(
                RolePermission.organization_id == org_id
            )
        )
    ).all()
    result: dict[uuid.UUID, set[str]] = {}
    for role_id, key in rows:
        result.setdefault(role_id, set()).add(key)
    return {k: frozenset(v) for k, v in result.items()}


async def user_role_keys(
    db: AsyncSession, org_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(UserRole.user_id, Role.key)
            .join(Role, (Role.id == UserRole.role_id) & (Role.organization_id == UserRole.organization_id))
            .where(UserRole.organization_id == org_id, UserRole.user_id.in_(user_ids))
            .order_by(Role.key)
        )
    ).all()
    result: dict[uuid.UUID, list[str]] = {uid: [] for uid in user_ids}
    for uid, key in rows:
        result[uid].append(key)
    return result


async def user_permissions(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> frozenset[str]:
    rows = await db.scalars(
        select(RolePermission.permission_key)
        .join(
            UserRole,
            (UserRole.role_id == RolePermission.role_id)
            & (UserRole.organization_id == RolePermission.organization_id),
        )
        .where(UserRole.organization_id == org_id, UserRole.user_id == user_id)
        .distinct()
    )
    return frozenset(rows.all())


async def count_active_admins(db: AsyncSession, org_id: uuid.UUID, admin_role_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(UserRole)
        .join(User, (User.id == UserRole.user_id) & (User.organization_id == UserRole.organization_id))
        .where(
            UserRole.organization_id == org_id,
            UserRole.role_id == admin_role_id,
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )
    return int(count or 0)


async def revoke_user_sessions(
    db: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    now: datetime,
    except_session_id: uuid.UUID | None = None,
) -> int:
    stmt = (
        update(UserSession)
        .where(
            UserSession.organization_id == org_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoke_reason=reason)
    )
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    result = await db.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)
