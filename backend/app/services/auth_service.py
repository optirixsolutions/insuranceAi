"""Registration, login, sessions, password changes and invitation/reset tokens."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
)
from app.core.tenancy import bind_tenant
from app.models.identity import LoginAttempt, Organization, Role, RolePermission, User, UserRole, UserSession
from app.repositories import identity as repo
from app.security.passwords import (
    PasswordPolicyError,
    burn_verification_time,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.security.permissions import ADMIN_ROLE_KEY, SYSTEM_ROLES
from app.security.principal import Principal
from app.security.throttle import LoginThrottle
from app.security.tokens import email_fingerprint, hash_token, new_token
from app.services import audit_service, mfa_service
from app.services.audit_service import AuditActor

INVALID_CREDENTIALS = "Invalid email or password"
_LAST_SEEN_GRANULARITY = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    token: str
    csrf_token: str
    absolute_expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "org"
    return f"{base}-{secrets.token_hex(3)}"


def _check_password(password: str, email: str) -> None:
    try:
        validate_password(password, email=email)
    except PasswordPolicyError as exc:
        raise BusinessRuleError(str(exc)) from exc


async def _issue_session(
    db: AsyncSession, settings: Settings, user: User, client: ClientInfo
) -> IssuedSession:
    now = _now()
    token, csrf = new_token(), new_token()
    absolute = now + timedelta(hours=settings.session_absolute_hours)
    session = UserSession(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_hash=hash_token(csrf),
        ip=client.ip,
        user_agent=(client.user_agent or "")[:300] or None,
        created_at=now,
        last_seen_at=now,
        idle_expires_at=min(now + timedelta(minutes=settings.session_idle_minutes), absolute),
        absolute_expires_at=absolute,
    )
    db.add(session)
    await db.flush()
    return IssuedSession(session.id, user.id, user.organization_id, token, csrf, absolute)


async def register(
    db: AsyncSession,
    settings: Settings,
    *,
    organization_name: str,
    display_name: str,
    email: str,
    password: str,
    client: ClientInfo,
) -> IssuedSession:
    if not settings.self_registration_allowed:
        raise ForbiddenError("Self-service registration is disabled")
    _check_password(password, email)

    org_id = uuid.uuid4()
    await bind_tenant(db, org_id)
    db.add(Organization(id=org_id, name=organization_name, slug=_slugify(organization_name)))
    await db.flush()

    roles: dict[str, Role] = {}
    for definition in SYSTEM_ROLES:
        role = Role(
            id=uuid.uuid4(),
            organization_id=org_id,
            key=definition.key,
            name=definition.name,
            description=definition.description,
            is_system=True,
        )
        roles[definition.key] = role
        db.add(role)
    await db.flush()
    for definition in SYSTEM_ROLES:
        for perm in definition.permissions:
            db.add(
                RolePermission(
                    organization_id=org_id, role_id=roles[definition.key].id, permission_key=perm.value
                )
            )

    now = _now()
    user = User(
        id=uuid.uuid4(),
        organization_id=org_id,
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
        status="active",
        password_changed_at=now,
        last_login_at=now,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # Deliberately generic: do not confirm which emails are registered.
        raise ConflictError("Registration could not be completed with these details") from exc
    db.add(
        UserRole(
            organization_id=org_id, user_id=user.id, role_id=roles[ADMIN_ROLE_KEY].id, granted_by=user.id
        )
    )

    actor = AuditActor("user", user.id, client.request_id, client.ip)
    await audit_service.record(
        db,
        actor=actor,
        action="organization.created",
        entity_type="organization",
        entity_id=org_id,
        after={"name": organization_name},
    )
    await audit_service.record(
        db,
        actor=actor,
        action="user.registered",
        entity_type="user",
        entity_id=user.id,
        after={"email": email, "roles": [ADMIN_ROLE_KEY]},
    )
    issued = await _issue_session(db, settings, user, client)
    await db.commit()
    return issued


async def login(
    db: AsyncSession,
    settings: Settings,
    throttle: LoginThrottle,
    *,
    email: str,
    password: str,
    client: ClientInfo,
) -> IssuedSession | MfaRequired:
    fingerprint = email_fingerprint(email)
    decision = await throttle.check(fingerprint, client.ip)
    if not decision.allowed:
        db.add(
            LoginAttempt(
                email_hash=fingerprint,
                ip=client.ip,
                user_agent=client.user_agent,
                success=False,
                reason="throttled",
            )
        )
        await db.commit()
        raise RateLimitedError(
            "Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    found = await repo.find_user_for_login(db, email)
    if found is None:
        burn_verification_time(password)
        await throttle.record_failure(fingerprint, client.ip)
        db.add(
            LoginAttempt(
                email_hash=fingerprint,
                ip=client.ip,
                user_agent=client.user_agent,
                success=False,
                reason="unknown_account",
            )
        )
        await db.commit()
        raise UnauthenticatedError(INVALID_CREDENTIALS)

    user_id, org_id = found
    await bind_tenant(db, org_id)
    user = await repo.get_user(db, org_id, user_id)
    password_ok = verify_password(user.password_hash if user else None, password)
    if user is None or not password_ok or user.status != "active":
        reason = "bad_password" if not password_ok else f"status_{user.status if user else 'missing'}"
        await throttle.record_failure(fingerprint, client.ip)
        db.add(
            LoginAttempt(
                organization_id=org_id,
                user_id=user_id,
                email_hash=fingerprint,
                ip=client.ip,
                user_agent=client.user_agent,
                success=False,
                reason=reason,
            )
        )
        await audit_service.record(
            db,
            actor=AuditActor("user", user_id, client.request_id, client.ip),
            action="auth.login_failed",
            entity_type="user",
            entity_id=user_id,
            after={"reason": reason},
        )
        await db.commit()
        raise UnauthenticatedError(INVALID_CREDENTIALS)

    await throttle.record_success(fingerprint)
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    if await mfa_service.has_factor(db, user.id):  # Phase 13: second step before any session exists
        token = await mfa_service.start_challenge(db, user, client.ip)
        db.add(
            LoginAttempt(
                organization_id=org_id,
                user_id=user_id,
                email_hash=fingerprint,
                ip=client.ip,
                user_agent=client.user_agent,
                success=False,
                reason="mfa_challenge",
            )
        )
        await db.commit()
        return MfaRequired(token)
    return await _finish_login(db, settings, user, client, fingerprint, method="password")


@dataclass(frozen=True, slots=True)
class MfaRequired:
    challenge: str


async def complete_mfa(
    db: AsyncSession, settings: Settings, *, challenge: str, code: str, client: ClientInfo
) -> IssuedSession:
    verified = await mfa_service.verify_challenge(db, settings, challenge, code)
    user = verified.user
    if verified.method == "recovery":
        await audit_service.record(
            db,
            actor=AuditActor("user", user.id, client.request_id, client.ip),
            action="auth.mfa_recovery_code_used",
            entity_type="user",
            entity_id=user.id,
        )
    return await _finish_login(
        db, settings, user, client, email_fingerprint(user.email), method=f"mfa_{verified.method}"
    )


async def _finish_login(
    db: AsyncSession, settings: Settings, user: User, client: ClientInfo, fingerprint: bytes, *, method: str
) -> IssuedSession:
    org_id, user_id = user.organization_id, user.id
    user.last_login_at = _now()
    issued = await _issue_session(db, settings, user, client)
    db.add(
        LoginAttempt(
            organization_id=org_id,
            user_id=user_id,
            email_hash=fingerprint,
            ip=client.ip,
            user_agent=client.user_agent,
            success=True,
            reason="ok",
        )
    )
    await audit_service.record(
        db,
        actor=AuditActor("user", user_id, client.request_id, client.ip),
        action="auth.login_succeeded",
        entity_type="user_session",
        entity_id=issued.session_id,
        after={"method": method},
    )
    await db.commit()
    return issued


async def resolve_session(db: AsyncSession, settings: Settings, token: str) -> Principal | None:
    found = await repo.find_session(db, hash_token(token))
    if found is None:
        return None
    session_id, org_id = found
    await bind_tenant(db, org_id)
    session = await db.scalar(select(UserSession).where(UserSession.id == session_id))
    if session is None or session.revoked_at is not None:
        return None
    user = await repo.get_user(db, org_id, session.user_id)
    now = _now()
    if user is None or user.status != "active":
        return None
    if now >= session.idle_expires_at or now >= session.absolute_expires_at:
        session.revoked_at, session.revoke_reason = now, "expired"
        await db.commit()
        return None
    if now - session.last_seen_at >= _LAST_SEEN_GRANULARITY:
        session.last_seen_at = now
        session.idle_expires_at = min(
            now + timedelta(minutes=settings.session_idle_minutes), session.absolute_expires_at
        )
        await db.commit()

    roles = await repo.user_role_keys(db, org_id, [user.id])
    role_keys = frozenset(roles.get(user.id, []))
    return Principal(
        user_id=user.id,
        organization_id=org_id,
        session_id=session.id,
        email=user.email,
        display_name=user.display_name,
        role_keys=role_keys,
        permissions=await repo.user_permissions(db, org_id, user.id),
        csrf_hash=session.csrf_hash,
        mfa_enrollment_required=await mfa_service.enrollment_required(db, org_id, user.id, role_keys),
    )


async def logout(db: AsyncSession, principal: Principal, client: ClientInfo) -> None:
    session = await db.scalar(select(UserSession).where(UserSession.id == principal.session_id))
    if session is not None and session.revoked_at is None:
        session.revoked_at, session.revoke_reason = _now(), "logout"
        await audit_service.record(
            db,
            actor=AuditActor("user", principal.user_id, client.request_id, client.ip),
            action="auth.logout",
            entity_type="user_session",
            entity_id=session.id,
        )
    await db.commit()


async def change_password(
    db: AsyncSession, principal: Principal, *, current_password: str, new_password: str, client: ClientInfo
) -> None:
    user = await repo.get_user(db, principal.organization_id, principal.user_id)
    if user is None or not verify_password(user.password_hash, current_password):
        raise UnauthenticatedError("Current password is incorrect")
    _check_password(new_password, user.email)
    now = _now()
    user.password_hash, user.password_changed_at = hash_password(new_password), now
    revoked = await repo.revoke_user_sessions(
        db,
        user.organization_id,
        user.id,
        reason="password_changed",
        now=now,
        except_session_id=principal.session_id,
    )
    await audit_service.record(
        db,
        actor=AuditActor("user", user.id, client.request_id, client.ip),
        action="auth.password_changed",
        entity_type="user",
        entity_id=user.id,
        after={"other_sessions_revoked": revoked},
    )
    await db.commit()


async def accept_access_token(
    db: AsyncSession, settings: Settings, *, token: str, password: str, client: ClientInfo
) -> IssuedSession:
    """Complete an invitation or password reset and sign the user in."""
    found = await repo.find_access_token(db, hash_token(token))
    if found is None:
        raise BusinessRuleError("This link is invalid or has already been used")
    user_id, org_id = found
    await bind_tenant(db, org_id)
    user = await repo.get_user(db, org_id, user_id)
    now = _now()
    if user is None or user.access_token_expires_at is None or user.access_token_expires_at <= now:
        raise BusinessRuleError("This link has expired. Ask an administrator for a new one.")
    if user.status == "disabled":
        raise BusinessRuleError("This account is disabled")
    _check_password(password, user.email)
    purpose = user.access_token_purpose
    user.password_hash, user.password_changed_at = hash_password(password), now
    user.status, user.last_login_at = "active", now
    user.access_token_hash = user.access_token_purpose = user.access_token_expires_at = None
    await repo.revoke_user_sessions(db, org_id, user.id, reason="access_reset", now=now)
    await audit_service.record(
        db,
        actor=AuditActor("user", user.id, client.request_id, client.ip),
        action=f"auth.{purpose or 'access'}_accepted",
        entity_type="user",
        entity_id=user.id,
    )
    issued = await _issue_session(db, settings, user, client)
    await db.commit()
    return issued


async def list_sessions(db: AsyncSession, principal: Principal) -> list[UserSession]:
    rows = await db.scalars(
        select(UserSession)
        .where(
            UserSession.organization_id == principal.organization_id,
            UserSession.user_id == principal.user_id,
            UserSession.revoked_at.is_(None),
        )
        .order_by(UserSession.last_seen_at.desc())
    )
    return list(rows.all())


async def revoke_own_session(
    db: AsyncSession, principal: Principal, session_id: uuid.UUID, client: ClientInfo
) -> None:
    session = await db.scalar(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == principal.user_id,
            UserSession.organization_id == principal.organization_id,
        )
    )
    if session is None or session.revoked_at is not None:
        raise NotFoundError("Session not found")
    session.revoked_at, session.revoke_reason = _now(), "user_revoked"
    await audit_service.record(
        db,
        actor=AuditActor("user", principal.user_id, client.request_id, client.ip),
        action="auth.session_revoked",
        entity_type="user_session",
        entity_id=session.id,
    )
    await db.commit()
