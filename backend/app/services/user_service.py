"""User administration within one organisation.

Escalation rules:
* A user can only grant or revoke roles whose permissions they hold themselves.
* Users cannot change their own roles or status.
* The organisation always keeps at least one active administrator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PreconditionFailedError,
)
from app.models.identity import LoginAttempt, Role, User, UserRole
from app.repositories import identity as repo
from app.security.permissions import ADMIN_ROLE_KEY
from app.security.principal import Principal
from app.security.tokens import hash_token, new_token
from app.services import audit_service
from app.services.audit_service import AuditActor


@dataclass(frozen=True, slots=True)
class UserView:
    user: User
    role_keys: list[str]


@dataclass(frozen=True, slots=True)
class AccessLink:
    token: str
    purpose: Literal["invite", "reset"]
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


async def _load(db: AsyncSession, principal: Principal, user_id: uuid.UUID) -> User:
    user = await repo.get_user(db, principal.organization_id, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


def _check_version(user: User, expected_version: int) -> None:
    if user.row_version != expected_version:
        raise PreconditionFailedError("This user was changed by someone else. Reload and try again.")


def _issue_access_token(user: User, purpose: Literal["invite", "reset"], settings: Settings) -> AccessLink:
    token = new_token()
    expires = _now() + timedelta(hours=settings.access_token_ttl_hours)
    user.access_token_hash, user.access_token_purpose, user.access_token_expires_at = (
        hash_token(token),
        purpose,
        expires,
    )
    return AccessLink(token, purpose, expires)


async def _assert_can_assign(db: AsyncSession, principal: Principal, role_keys: set[str]) -> dict[str, Role]:
    roles = await repo.roles_by_key(db, principal.organization_id)
    unknown = role_keys - roles.keys()
    if unknown:
        raise BusinessRuleError(f"Unknown role(s): {', '.join(sorted(unknown))}")
    perms = await repo.role_permissions(db, principal.organization_id)
    for key in sorted(role_keys):
        if not principal.holds_all(perms.get(roles[key].id, frozenset())):
            raise ForbiddenError(
                f"You cannot grant or revoke the role '{key}' because it exceeds your own permissions"
            )
    return roles


async def _assert_admin_remains(db: AsyncSession, principal: Principal, roles: dict[str, Role]) -> None:
    if await repo.count_active_admins(db, principal.organization_id, roles[ADMIN_ROLE_KEY].id) < 1:
        raise BusinessRuleError("The organisation must keep at least one active administrator")


async def list_users(
    db: AsyncSession, principal: Principal, *, page: int, page_size: int, q: str | None, status: str | None
) -> tuple[list[UserView], int]:
    conditions = [User.organization_id == principal.organization_id, User.deleted_at.is_(None)]
    if status:
        conditions.append(User.status == status)
    if q:
        pattern = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        conditions.append(or_(User.email.ilike(pattern), User.display_name.ilike(pattern)))
    total = int(await db.scalar(select(func.count()).select_from(User).where(*conditions)) or 0)
    users = (
        await db.scalars(
            select(User)
            .where(*conditions)
            .order_by(User.display_name, User.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    keys = await repo.user_role_keys(db, principal.organization_id, [u.id for u in users])
    return [UserView(u, keys.get(u.id, [])) for u in users], total


async def get_user(db: AsyncSession, principal: Principal, user_id: uuid.UUID) -> UserView:
    user = await _load(db, principal, user_id)
    keys = await repo.user_role_keys(db, principal.organization_id, [user.id])
    return UserView(user, keys.get(user.id, []))


async def invite_user(
    db: AsyncSession,
    settings: Settings,
    principal: Principal,
    actor: AuditActor,
    *,
    email: str,
    display_name: str,
    role_keys: set[str],
) -> tuple[UserView, AccessLink]:
    if not role_keys:
        raise BusinessRuleError("Assign at least one role")
    roles = await _assert_can_assign(db, principal, role_keys)
    user = User(
        id=uuid.uuid4(),
        organization_id=principal.organization_id,
        email=email,
        display_name=display_name,
        status="invited",
    )
    link = _issue_access_token(user, "invite", settings)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("A user with this email address already exists") from exc
    for key in sorted(role_keys):
        db.add(
            UserRole(
                organization_id=principal.organization_id,
                user_id=user.id,
                role_id=roles[key].id,
                granted_by=principal.user_id,
            )
        )
    await audit_service.record(
        db,
        actor=actor,
        action="user.invited",
        entity_type="user",
        entity_id=user.id,
        after={"email": email, "display_name": display_name, "roles": sorted(role_keys)},
    )
    await db.commit()
    return UserView(user, sorted(role_keys)), link


async def update_user(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    user_id: uuid.UUID,
    *,
    expected_version: int,
    display_name: str | None,
    status: Literal["active", "disabled"] | None,
) -> UserView:
    user = await _load(db, principal, user_id)
    _check_version(user, expected_version)
    before = {"display_name": user.display_name, "status": user.status}
    if display_name is not None:
        user.display_name = display_name
    if status is not None and status != user.status:
        if user.id == principal.user_id:
            raise ForbiddenError("You cannot change your own account status")
        if status == "disabled":
            user.status = "disabled"
            await repo.revoke_user_sessions(
                db, user.organization_id, user.id, reason="user_disabled", now=_now()
            )
            await db.flush()
            await _assert_admin_remains(db, principal, await repo.roles_by_key(db, principal.organization_id))
        else:
            # Re-enabling: users who never set a password return to "invited".
            user.status = "active" if user.password_hash else "invited"
    user.row_version += 1
    after = {"display_name": user.display_name, "status": user.status}
    if after != before:
        await audit_service.record(
            db,
            actor=actor,
            action="user.updated",
            entity_type="user",
            entity_id=user.id,
            before=before,
            after=after,
        )
    await db.commit()
    return await get_user(db, principal, user.id)


async def set_roles(
    db: AsyncSession, principal: Principal, actor: AuditActor, user_id: uuid.UUID, role_keys: set[str]
) -> UserView:
    if user_id == principal.user_id:
        raise ForbiddenError("You cannot change your own roles")
    if not role_keys:
        raise BusinessRuleError("Assign at least one role")
    user = await _load(db, principal, user_id)
    current = set((await repo.user_role_keys(db, principal.organization_id, [user.id])).get(user.id, []))
    added, removed = role_keys - current, current - role_keys
    roles = await _assert_can_assign(db, principal, added | removed)
    for key in removed:
        await db.execute(
            delete(UserRole).where(
                UserRole.organization_id == principal.organization_id,
                UserRole.user_id == user.id,
                UserRole.role_id == roles[key].id,
            )
        )
    for key in added:
        db.add(
            UserRole(
                organization_id=principal.organization_id,
                user_id=user.id,
                role_id=roles[key].id,
                granted_by=principal.user_id,
            )
        )
    await db.flush()
    if ADMIN_ROLE_KEY in removed:
        await _assert_admin_remains(db, principal, await repo.roles_by_key(db, principal.organization_id))
    if added or removed:
        await audit_service.record(
            db,
            actor=actor,
            action="user.roles_changed",
            entity_type="user",
            entity_id=user.id,
            before={"roles": sorted(current)},
            after={"roles": sorted(role_keys)},
        )
    await db.commit()
    return await get_user(db, principal, user.id)


async def reset_access(
    db: AsyncSession, settings: Settings, principal: Principal, actor: AuditActor, user_id: uuid.UUID
) -> AccessLink:
    user = await _load(db, principal, user_id)
    if user.status == "disabled":
        raise BusinessRuleError("Enable the user before resetting access")
    purpose: Literal["invite", "reset"] = "reset" if user.password_hash else "invite"
    link = _issue_access_token(user, purpose, settings)
    revoked = await repo.revoke_user_sessions(
        db, user.organization_id, user.id, reason="access_reset", now=_now()
    )
    user.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="user.access_reset",
        entity_type="user",
        entity_id=user.id,
        after={"purpose": purpose, "sessions_revoked": revoked},
    )
    await db.commit()
    return link


async def login_activity(
    db: AsyncSession, principal: Principal, user_id: uuid.UUID, limit: int
) -> list[LoginAttempt]:
    await _load(db, principal, user_id)
    rows = await db.scalars(
        select(LoginAttempt)
        .where(LoginAttempt.organization_id == principal.organization_id, LoginAttempt.user_id == user_id)
        .order_by(LoginAttempt.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())
