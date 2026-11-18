"""Domain events (Phase 11): one call writes the webhook deliveries and notifications of an event in
the caller's transaction. Sending happens after commit (dispatch tasks), never inside it."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claims import Claim, ClaimAssignment
from app.models.communications import Notification
from app.models.identity import User
from app.repositories import identity as identity_repo
from app.services import webhook_service


@dataclass(frozen=True, slots=True)
class Notice:
    user_ids: Iterable[uuid.UUID]
    template_key: str
    title: str
    body: str
    link: str | None = None
    email: bool = False


@dataclass
class EmitResult:
    deliveries: int = 0
    notifications: list[uuid.UUID] = field(default_factory=list)


async def emit(
    s: AsyncSession,
    organization_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any],
    *,
    claim_id: uuid.UUID | None = None,
    notices: Iterable[Notice] = (),
) -> EmitResult:
    result = EmitResult()
    result.deliveries = await webhook_service.enqueue(s, organization_id, event_type, data, uuid.uuid4())
    now = datetime.now(UTC)
    for notice in notices:
        for user_id in dict.fromkeys(notice.user_ids):  # each person once, order kept
            channels = ("in_app", "email") if notice.email else ("in_app",)
            for channel in channels:
                nid = uuid.uuid4()
                s.add(
                    Notification(
                        id=nid,
                        organization_id=organization_id,
                        user_id=user_id,
                        channel=channel,
                        template_key=notice.template_key,
                        title=notice.title[:200],
                        body=notice.body[:2000],
                        link=notice.link,
                        claim_id=claim_id,
                        payload={"event_type": event_type},
                        status="sent" if channel == "in_app" else "pending",
                        provider="in_app" if channel == "in_app" else None,
                        created_at=now,
                        sent_at=now if channel == "in_app" else None,
                    )
                )
                result.notifications.append(nid)
    return result


async def holders_of(s: AsyncSession, organization_id: uuid.UUID, permission: str) -> list[uuid.UUID]:
    users = await s.scalars(
        select(User.id).where(
            User.organization_id == organization_id, User.status == "active", User.deleted_at.is_(None)
        )
    )
    return [u for u in users if permission in await identity_repo.user_permissions(s, organization_id, u)]


async def claim_people(s: AsyncSession, claim: Claim) -> list[uuid.UUID]:
    """The claim's current adjuster(s) and its creator: who should hear about its outcome."""
    assigned = await s.scalars(
        select(ClaimAssignment.user_id).where(
            ClaimAssignment.claim_id == claim.id,
            ClaimAssignment.role_on_claim == "adjuster",
            ClaimAssignment.unassigned_at.is_(None),
        )
    )
    return [*assigned, *([claim.created_by] if claim.created_by else [])]


def claim_data(claim: Claim, **extra: Any) -> dict[str, Any]:
    """Webhook payloads carry identifiers and outcomes only, never customer details or text."""
    return {
        "claim_id": str(claim.id),
        "claim_reference": claim.claim_reference,
        "status": claim.status,
        **extra,
    }
