"""Object-level access to claims (docs/architecture/04-security-model.md §3).

* Organisation-wide visibility: users who assign claims (managers, admins) and oversight roles
  (auditors, compliance officers).
* Everyone else with ``claims.read`` (adjusters, reviewers, investigators) sees only claims
  currently assigned to them. A claim they cannot see answers 404, not 403.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.models.claims import Claim, ClaimAssignment
from app.security.permissions import P
from app.security.principal import Principal

_ORG_WIDE = frozenset({P.CLAIMS_ASSIGN.value, P.AUDIT_READ.value, P.COMPLIANCE_MANAGE.value})


def sees_all_claims(principal: Principal) -> bool:
    return bool(principal.permissions & _ORG_WIDE)


def _assigned_to(user_id: uuid.UUID) -> object:
    return exists().where(
        ClaimAssignment.claim_id == Claim.id,
        ClaimAssignment.user_id == user_id,
        ClaimAssignment.unassigned_at.is_(None),
    )


def scope_claims(stmt: Any, principal: Principal) -> Any:
    """Restrict any SELECT that includes ``Claim`` to the caller's visible claims."""
    stmt = stmt.where(Claim.organization_id == principal.organization_id, Claim.deleted_at.is_(None))
    if not sees_all_claims(principal):
        stmt = stmt.where(_assigned_to(principal.user_id))
    return stmt


async def get_visible_claim(
    db: AsyncSession, principal: Principal, claim_id: uuid.UUID, *, lock: bool = False
) -> Claim:
    stmt = scope_claims(select(Claim).where(Claim.id == claim_id), principal)
    if lock:
        stmt = stmt.with_for_update(key_share=True)
    claim: Claim | None = await db.scalar(stmt)
    if claim is None:
        raise NotFoundError("Claim not found")
    return claim


async def get_writable_claim(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> Claim:
    claim = await get_visible_claim(db, principal, claim_id, lock=True)
    if not principal.has(P.CLAIMS_WRITE.value):
        raise ForbiddenError("This action requires the 'claims.write' permission")
    if not (principal.has(P.CLAIMS_ASSIGN.value) or await _is_assigned(db, claim.id, principal.user_id)):
        raise ForbiddenError("Only the assigned handler or a claims manager can change this claim")
    return claim


async def _is_assigned(db: AsyncSession, claim_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(
                    ClaimAssignment.claim_id == claim_id,
                    ClaimAssignment.user_id == user_id,
                    ClaimAssignment.unassigned_at.is_(None),
                )
            )
        )
    )
