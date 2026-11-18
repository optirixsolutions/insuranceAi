"""Two-factor authentication with TOTP (Phase 13).

* Enrolment: ``setup`` creates an unconfirmed factor (secret encrypted at rest, bound to the user id) and
  returns the base32 secret and ``otpauth://`` URI once; ``confirm`` checks a first code, activates the
  factor and returns ten one-time recovery codes (only their SHA-256 hashes are stored).
* Sign-in: after a correct password, a user with a confirmed factor gets a challenge instead of a session.
  The challenge token is ``<org id>.<random>`` (only its hash is stored), lives five minutes and allows five
  attempts. A TOTP code must belong to a later time step than the last accepted one (no replays); a recovery
  code works once.
* Policy: an organisation can require MFA for chosen roles. Until such a user enrols, every permission check
  refuses (enforced in ``app.api.deps.require``); enrolment itself only needs a session.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.config import Settings
from app.core.errors import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
)
from app.core.tenancy import bind_tenant
from app.models.identity import Organization, User
from app.models.security import MfaChallenge, UserMfa
from app.repositories import identity as repo
from app.security import totp
from app.security.passwords import verify_password
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor

CHALLENGE_MINUTES = 5
MAX_ATTEMPTS = 5
RECOVERY_CODES = 10
INVALID_CODE = "The code is not valid. Check your authenticator app and try again."


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().replace("-", "").replace(" ", "").encode()).hexdigest()


async def _factor(db: AsyncSession, user_id: uuid.UUID) -> UserMfa | None:
    factor: UserMfa | None = await db.scalar(select(UserMfa).where(UserMfa.user_id == user_id))
    return factor


async def required_for(db: AsyncSession, organization_id: uuid.UUID, role_keys: frozenset[str]) -> bool:
    org = await db.get(Organization, organization_id)
    return bool(org and set(org.mfa_required_roles or []) & role_keys)


async def enrollment_required(
    db: AsyncSession, principal_org: uuid.UUID, user_id: uuid.UUID, roles: frozenset[str]
) -> bool:
    if not await required_for(db, principal_org, roles):
        return False
    factor = await _factor(db, user_id)
    return factor is None or factor.confirmed_at is None


async def status(db: AsyncSession, principal: Principal) -> dict[str, Any]:
    factor = await _factor(db, principal.user_id)
    confirmed = factor is not None and factor.confirmed_at is not None
    return {
        "enabled": confirmed,
        "confirmed_at": factor.confirmed_at.isoformat() if factor and factor.confirmed_at else None,
        "recovery_codes_left": len(factor.recovery_hashes) if confirmed and factor else 0,
        "required": await required_for(db, principal.organization_id, principal.role_keys),
    }


async def setup(db: AsyncSession, principal: Principal, settings: Settings) -> dict[str, str]:
    factor = await _factor(db, principal.user_id)
    if factor is not None and factor.confirmed_at is not None:
        raise ConflictError("Two-factor authentication is already on. Turn it off first to replace it.")
    secret = totp.new_secret()
    encrypted = crypto.encrypt(settings, secret, context=f"mfa:{principal.user_id}")
    if factor is None:
        db.add(
            UserMfa(
                user_id=principal.user_id, organization_id=principal.organization_id, secret_enc=encrypted
            )
        )
    else:
        factor.secret_enc, factor.last_used_step, factor.recovery_hashes = encrypted, 0, []
    await db.flush()
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, principal.email)}


def _secret(settings: Settings, factor: UserMfa) -> str:
    return crypto.decrypt(settings, factor.secret_enc, context=f"mfa:{factor.user_id}")


async def confirm(
    db: AsyncSession, principal: Principal, actor: AuditActor, settings: Settings, code: str
) -> list[str]:
    factor = await _factor(db, principal.user_id)
    if factor is None:
        raise NotFoundError("Start the set-up first")
    if factor.confirmed_at is not None:
        raise ConflictError("Two-factor authentication is already on")
    step = totp.match(_secret(settings, factor), code)
    if step is None:
        raise BusinessRuleError(INVALID_CODE)
    codes = ["-".join(secrets.token_hex(4)[i : i + 4] for i in (0, 4)) for _ in range(RECOVERY_CODES)]
    factor.confirmed_at, factor.last_used_step = _now(), step
    factor.recovery_hashes = [_hash(c) for c in codes]
    await audit_service.record(
        db, actor=actor, action="auth.mfa_enabled", entity_type="user", entity_id=principal.user_id
    )
    return codes


async def disable(
    db: AsyncSession, principal: Principal, actor: AuditActor, settings: Settings, *, password: str, code: str
) -> None:
    factor = await _factor(db, principal.user_id)
    if factor is None or factor.confirmed_at is None:
        raise NotFoundError("Two-factor authentication is not on")
    if await required_for(db, principal.organization_id, principal.role_keys):
        raise ForbiddenError("Your organisation requires two-factor authentication for your role")
    user = await repo.get_user(db, principal.organization_id, principal.user_id)
    if user is None or not verify_password(user.password_hash, password):
        raise BusinessRuleError("Your password is not correct")
    if _use_code(settings, factor, code) is None:
        raise BusinessRuleError(INVALID_CODE)
    await db.delete(factor)
    await audit_service.record(
        db, actor=actor, action="auth.mfa_disabled", entity_type="user", entity_id=principal.user_id
    )


async def admin_reset(db: AsyncSession, principal: Principal, actor: AuditActor, user_id: uuid.UUID) -> None:
    """A user manager removes someone's factor (lost phone); they enrol again at next sign-in."""
    if user_id == principal.user_id:
        raise ForbiddenError("Reset your own factor from your security settings")
    if await repo.get_user(db, principal.organization_id, user_id) is None:
        raise NotFoundError("User not found")
    factor = await _factor(db, user_id)
    if factor is not None:
        await db.delete(factor)
    await audit_service.record(
        db, actor=actor, action="auth.mfa_reset", entity_type="user", entity_id=user_id
    )


def _use_code(settings: Settings, factor: UserMfa, code: str) -> str | None:
    """'totp' or 'recovery' if the code is accepted (and consumed), else None."""
    step = totp.match(_secret(settings, factor), code)
    if step is not None:
        if step <= factor.last_used_step:
            return None  # already used: a replayed code
        factor.last_used_step = step
        return "totp"
    digest = _hash(code)
    if digest in factor.recovery_hashes:
        factor.recovery_hashes = [h for h in factor.recovery_hashes if h != digest]
        return "recovery"
    return None


async def has_factor(db: AsyncSession, user_id: uuid.UUID) -> bool:
    factor = await _factor(db, user_id)
    return factor is not None and factor.confirmed_at is not None


async def start_challenge(db: AsyncSession, user: User, ip: str | None) -> str:
    secret = secrets.token_urlsafe(32)
    db.add(
        MfaChallenge(
            id=uuid.uuid4(),
            organization_id=user.organization_id,
            user_id=user.id,
            token_hash=hashlib.sha256(secret.encode()).digest(),
            expires_at=_now() + timedelta(minutes=CHALLENGE_MINUTES),
            ip=ip,
        )
    )
    await db.flush()
    return f"{user.organization_id}.{secret}"


@dataclass(frozen=True, slots=True)
class Verified:
    user: User
    method: str


async def verify_challenge(db: AsyncSession, settings: Settings, token: str, code: str) -> Verified:
    """Raises Unauthenticated for anything but a valid, unused, unexpired challenge and a good code."""
    org_part, _, secret = token.partition(".")
    try:
        org_id = uuid.UUID(org_part)
    except ValueError:
        raise UnauthenticatedError("Sign in again") from None
    await bind_tenant(db, org_id)
    challenge = await db.scalar(
        select(MfaChallenge)
        .where(MfaChallenge.token_hash == hashlib.sha256(secret.encode()).digest())
        .with_for_update()
    )
    if challenge is None or challenge.used_at is not None or challenge.expires_at < _now():
        raise UnauthenticatedError("This sign-in has expired. Sign in again.")
    if challenge.attempts >= MAX_ATTEMPTS:
        raise UnauthenticatedError("Too many wrong codes. Sign in again.")
    challenge.attempts += 1
    factor = await _factor(db, challenge.user_id)
    user = await repo.get_user(db, org_id, challenge.user_id)
    method = _use_code(settings, factor, code) if factor and factor.confirmed_at else None
    if user is None or user.status != "active" or method is None:
        await db.commit()  # keep the attempt count
        raise UnauthenticatedError(INVALID_CODE)
    challenge.used_at = _now()
    return Verified(user, method)


async def set_policy(
    db: AsyncSession, principal: Principal, actor: AuditActor, roles: list[str]
) -> list[str]:
    org = await db.get(Organization, principal.organization_id)
    assert org is not None
    before = list(org.mfa_required_roles or [])
    org.mfa_required_roles = sorted(set(roles))
    await audit_service.record(
        db,
        actor=actor,
        action="org.mfa_policy_changed",
        entity_type="organization",
        entity_id=org.id,
        before={"roles": before},
        after={"roles": org.mfa_required_roles},
    )
    return org.mfa_required_roles
