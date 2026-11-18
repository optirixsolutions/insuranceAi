"""Reviewer decision authorities (``/users/{id}/authorities``): replaced as a whole, audited."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ForbiddenError, NotFoundError
from app.models.rules import ReviewerAuthority
from app.repositories import identity as identity_repo
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from app.services.review_service import _require, authorities


@dataclass(frozen=True, slots=True)
class AuthorityInput:
    max_approval_amount: Decimal
    currency: str
    product_id: uuid.UUID | None
    can_reject: bool
    can_override_fraud: bool
    valid_from: date
    valid_to: date | None


def authority_view(a: ReviewerAuthority) -> dict[str, Any]:
    return {
        "max_approval_amount": str(a.max_approval_amount),
        "currency": a.currency,
        "product_id": str(a.product_id) if a.product_id else None,
        "can_reject": a.can_reject,
        "can_override_fraud": a.can_override_fraud,
        "valid_from": a.valid_from.isoformat(),
        "valid_to": a.valid_to.isoformat() if a.valid_to else None,
    }


async def replace_authorities(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    user_id: uuid.UUID,
    items: list[AuthorityInput],
) -> list[ReviewerAuthority]:
    _require(principal, P.USERS_MANAGE)
    if user_id == principal.user_id:
        raise ForbiddenError("You cannot change your own decision authority")
    if await identity_repo.get_user(db, principal.organization_id, user_id) is None:
        raise NotFoundError("User not found")
    if any(i.valid_to is not None and i.valid_to < i.valid_from for i in items):
        raise BusinessRuleError("An authority cannot end before it starts")
    old = await authorities(db, user_id)
    before = [authority_view(a) for a in old]
    for a in old:
        await db.delete(a)
    await db.flush()
    rows = [
        ReviewerAuthority(
            id=uuid.uuid4(),
            organization_id=principal.organization_id,
            user_id=user_id,
            product_id=i.product_id,
            max_approval_amount=i.max_approval_amount,
            currency=i.currency.upper(),
            can_reject=i.can_reject,
            can_override_fraud=i.can_override_fraud,
            valid_from=i.valid_from,
            valid_to=i.valid_to,
            granted_by=principal.user_id,
            created_at=datetime.now(UTC),
        )
        for i in items
    ]
    db.add_all(rows)
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="user.authorities_replaced",
        entity_type="user",
        entity_id=user_id,
        before={"authorities": before},
        after={"authorities": [authority_view(a) for a in rows]},
    )
    return rows
