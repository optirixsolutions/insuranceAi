"""Notifications (Phase 11): the in-app centre and email/SMS dispatch.

Notifications are written in the transaction of the event that causes them (``events.emit``). In-app
ones are delivered on insert; email and SMS ones are ``pending`` until the dispatch task sends them
(every 10 s via beat, also callable directly), so a failed send never rolls back the business change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import NotFoundError
from app.core.tenancy import bind_tenant
from app.integrations import messaging
from app.models.communications import Notification
from app.models.identity import User
from app.security.principal import Principal

MAX_ATTEMPTS_ERROR = "send failed"


def _now() -> datetime:
    return datetime.now(UTC)


async def inbox(
    db: AsyncSession, principal: Principal, *, unread_only: bool = False, limit: int = 50
) -> tuple[list[Notification], int]:
    mine = (
        Notification.organization_id == principal.organization_id,
        Notification.user_id == principal.user_id,
        Notification.channel == "in_app",
    )
    stmt = select(Notification).where(*mine)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = list(
        (await db.scalars(stmt.order_by(Notification.created_at.desc()).limit(min(limit, 200)))).all()
    )
    unread = int(await db.scalar(select(func.count()).where(*mine, Notification.read_at.is_(None))) or 0)
    return rows, unread


async def mark_read(db: AsyncSession, principal: Principal, notification_id: uuid.UUID) -> Notification:
    n = await db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.organization_id == principal.organization_id,
            Notification.user_id == principal.user_id,
            Notification.channel == "in_app",
        )
    )
    if n is None:
        raise NotFoundError("Notification not found")
    if n.read_at is None:
        n.read_at, n.status = _now(), "read"
    return n


async def mark_all_read(db: AsyncSession, principal: Principal) -> int:
    result = await db.execute(
        update(Notification)
        .where(
            Notification.organization_id == principal.organization_id,
            Notification.user_id == principal.user_id,
            Notification.channel == "in_app",
            Notification.read_at.is_(None),
        )
        .values(read_at=_now(), status="read")
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def send_one(s: AsyncSession, settings: Settings, notification_id: uuid.UUID) -> str:
    """Send one pending email/SMS notification (row locked, so two dispatchers never both send it)."""
    n = await s.scalar(
        select(Notification).where(Notification.id == notification_id).with_for_update(skip_locked=True)
    )
    if n is None or n.status != "pending":
        return "skipped"
    to: str | None = None
    if n.channel == "email" and n.user_id is not None:
        user = await s.get(User, n.user_id)
        to = user.email if user and user.status == "active" else None
    elif n.channel == "sms":
        to = str(n.payload.get("to") or "") or None
    if not to:
        n.status, n.error = "skipped", "no active recipient"
        return n.status
    link = f"{str(settings.app_base_url).rstrip('/')}{n.link}" if n.link else None
    body = n.body + (f"\n\nOpen in ClaimOS: {link}" if link else "")
    if n.channel == "email":
        result = await messaging.send_email(settings, to, f"[ClaimOS] {n.title}", body)
    else:
        result = await messaging.send_sms(settings, to, f"ClaimOS: {n.title}")
    n.status, n.provider, n.provider_message_id, n.error = (
        result.status,
        result.provider,
        result.message_id,
        result.error,
    )
    if result.status == "sent":
        n.sent_at = _now()
    return n.status


async def dispatch_pending(db: Database, settings: Settings, max_rows: int = 50) -> dict[str, int]:
    async with db.sessionmaker() as s:
        rows = (
            await s.execute(
                text("SELECT notification_id, organization_id FROM claimos_pending_notifications(:n)"),
                {"n": max_rows},
            )
        ).all()
    counts: dict[str, int] = {}
    for notification_id, org in rows:
        async with db.sessionmaker() as s:
            await bind_tenant(s, org)
            status = await send_one(s, settings, notification_id)
            await s.commit()
        counts[status] = counts.get(status, 0) + 1
    return counts


def as_dict(n: Notification) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "template_key": n.template_key,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "claim_id": str(n.claim_id) if n.claim_id else None,
        "created_at": n.created_at.isoformat(),
        "read_at": n.read_at.isoformat() if n.read_at else None,
    }


async def test_message(
    db: AsyncSession, principal: Principal, settings: Settings, channel: str, to: str | None
) -> Notification:
    """Send a test email to the caller (or an SMS through the adapter) right away and record it."""
    if channel == "sms" and not to:
        from app.core.errors import BusinessRuleError

        raise BusinessRuleError("A test SMS needs a phone number")
    n = Notification(
        id=uuid.uuid4(),
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        channel=channel,
        template_key="integration.test",
        title="Test message",
        body="This is a test message from ClaimOS. No action is needed.",
        payload={"to": to} if channel == "sms" else {},
        status="pending",
        created_at=_now(),
    )
    db.add(n)
    await db.flush()
    await send_one(db, settings, n.id)
    return n
