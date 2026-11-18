"""Outbound webhooks (05 §2.12): subscriptions, signed delivery, retries.

* The signing secret is generated here, shown once, and stored encrypted (``app.core.crypto``).
* URLs must be ``https`` and resolve to public addresses; hosts in ``WEBHOOK_ALLOWED_HOSTS`` may use
  ``http`` and private addresses (development only, refused in production). Redirects are not followed.
* Each event creates one delivery per matching subscription in the event's transaction; delivery runs
  later (beat every 10 s or directly), with backoff 30 s x 4^(attempt-1), and is ``dead`` after the
  configured number of attempts. A 2xx answer is delivered; anything else is retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.config import Settings
from app.core.db import Database
from app.core.errors import BusinessRuleError, NotFoundError
from app.core.tenancy import bind_tenant
from app.integrations import webhook_signing
from app.models.communications import WebhookDelivery, WebhookSubscription
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor

EVENT_TYPES = (
    "claim.status_changed",
    "claim.decided",
    "claim.processing_failed",
    "review.opened",
    "review.escalated",
    "review.second_approval_requested",
    "report.ready",
)
BASE_BACKOFF_SECONDS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


# --- URL safety ----------------------------------------------------------------------------------


def check_url(settings: Settings, url: str) -> None:
    """Static checks at registration time (the address is checked again at every delivery)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = host in settings.webhook_private_hosts
    if not host or parts.username or parts.password:
        raise BusinessRuleError("Webhook URL must be an absolute URL without credentials")
    if parts.scheme != "https" and not (parts.scheme == "http" and allowed):
        raise BusinessRuleError("Webhook URL must use https")


async def _resolve_public(settings: Settings, url: str) -> None:
    host = (urlsplit(url).hostname or "").lower()
    if host in settings.webhook_private_hosts:
        return
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise BusinessRuleError(f"Webhook host resolves to a non-public address ({address})")


# --- subscriptions -------------------------------------------------------------------------------


async def create(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    settings: Settings,
    *,
    url: str,
    event_types: list[str],
    description: str,
) -> tuple[WebhookSubscription, str]:
    check_url(settings, url)
    unknown = sorted(set(event_types) - set(EVENT_TYPES))
    if unknown or not event_types:
        raise BusinessRuleError(
            f"Choose event types from {list(EVENT_TYPES)}" + (f"; unknown: {unknown}" if unknown else "")
        )
    secret = webhook_signing.new_secret()
    sub_id = uuid.uuid4()
    sub = WebhookSubscription(
        id=sub_id,
        organization_id=principal.organization_id,
        url=url,
        description=description,
        secret_enc=crypto.encrypt(settings, secret, context=str(sub_id)),
        secret_hint=secret[:10],
        event_types=sorted(set(event_types)),
        active=True,
        created_by=principal.user_id,
        created_at=_now(),
    )
    db.add(sub)
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="webhook.created",
        entity_type="webhook",
        entity_id=sub.id,
        after={"url": url, "event_types": sub.event_types},
    )
    return sub, secret


async def list_subscriptions(db: AsyncSession, principal: Principal) -> list[WebhookSubscription]:
    stmt = select(WebhookSubscription).where(WebhookSubscription.organization_id == principal.organization_id)
    return list((await db.scalars(stmt.order_by(WebhookSubscription.created_at))).all())


async def get(db: AsyncSession, principal: Principal, sub_id: uuid.UUID) -> WebhookSubscription:
    sub = await db.get(WebhookSubscription, sub_id)
    if sub is None or sub.organization_id != principal.organization_id:
        raise NotFoundError("Webhook not found")
    return sub


async def disable(db: AsyncSession, principal: Principal, actor: AuditActor, sub_id: uuid.UUID) -> None:
    sub = await get(db, principal, sub_id)
    sub.active, sub.disabled_at = False, _now()
    await audit_service.record(
        db,
        actor=actor,
        action="webhook.disabled",
        entity_type="webhook",
        entity_id=sub.id,
        after={"url": sub.url},
    )


async def deliveries(db: AsyncSession, principal: Principal, sub_id: uuid.UUID) -> list[WebhookDelivery]:
    sub = await get(db, principal, sub_id)
    stmt = select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub.id)
    return list((await db.scalars(stmt.order_by(WebhookDelivery.created_at.desc()).limit(100))).all())


# --- events and delivery -------------------------------------------------------------------------


async def enqueue(
    s: AsyncSession, organization_id: uuid.UUID, event_type: str, data: dict[str, Any], event_id: uuid.UUID
) -> int:
    """One pending delivery per active subscription for this event type (same transaction)."""
    subs = await s.scalars(
        select(WebhookSubscription).where(
            WebhookSubscription.organization_id == organization_id, WebhookSubscription.active.is_(True)
        )
    )
    created = 0
    now = _now()
    payload = {"id": str(event_id), "type": event_type, "created_at": now.isoformat(), "data": data}
    for sub in subs:
        if event_type not in sub.event_types:
            continue
        s.add(
            WebhookDelivery(
                id=uuid.uuid4(),
                organization_id=organization_id,
                subscription_id=sub.id,
                event_id=event_id,
                event_type=event_type,
                payload=payload,
                payload_sha256=hashlib.sha256(canonical(payload)).hexdigest(),
                attempt=0,
                status="pending",
                next_attempt_at=now,
                created_at=now,
            )
        )
        created += 1
    return created


async def deliver(s: AsyncSession, settings: Settings, delivery_id: uuid.UUID) -> str:
    d = await s.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update(skip_locked=True)
    )
    if d is None or d.status in ("delivered", "dead"):
        return "skipped"
    sub = await s.get(WebhookSubscription, d.subscription_id)
    d.attempt += 1
    if sub is None or not sub.active:
        d.status, d.last_error, d.next_attempt_at = "dead", "subscription disabled", None
        return d.status
    body = canonical(d.payload)
    secret = crypto.decrypt(settings, sub.secret_enc, context=str(sub.id))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ClaimOS-Webhooks/1",
        "X-ClaimOS-Event": d.event_type,
        "X-ClaimOS-Delivery": str(d.id),
        webhook_signing.HEADER: webhook_signing.sign(secret, body),
    }
    try:
        await _resolve_public(settings, sub.url)
        async with httpx.AsyncClient(
            timeout=settings.webhook_timeout_seconds, follow_redirects=False
        ) as http:
            response = await http.post(sub.url, content=body, headers=headers)
        d.response_code = response.status_code
        ok = 200 <= response.status_code < 300
        d.last_error = None if ok else f"HTTP {response.status_code}"
    except (httpx.HTTPError, OSError, BusinessRuleError) as exc:
        ok, d.response_code = False, None
        d.last_error = f"{type(exc).__name__}: {exc}"[:500]
    if ok:
        d.status, d.delivered_at, d.next_attempt_at = "delivered", _now(), None
    elif d.attempt >= settings.webhook_max_attempts:
        d.status, d.next_attempt_at = "dead", None
    else:
        d.status = "failed"
        d.next_attempt_at = _now() + timedelta(seconds=BASE_BACKOFF_SECONDS * 4 ** (d.attempt - 1))
    return d.status


async def dispatch_due(db: Database, settings: Settings, max_rows: int = 50) -> dict[str, int]:
    async with db.sessionmaker() as s:
        rows = (
            await s.execute(
                text("SELECT delivery_id, organization_id FROM claimos_due_webhook_deliveries(:n)"),
                {"n": max_rows},
            )
        ).all()
    counts: dict[str, int] = {}
    for delivery_id, org in rows:
        async with db.sessionmaker() as s:
            await bind_tenant(s, org)
            status = await deliver(s, settings, delivery_id)
            await s.commit()
        counts[status] = counts.get(status, 0) + 1
    return counts


async def ping(db: AsyncSession, principal: Principal, sub_id: uuid.UUID) -> WebhookDelivery:
    """A ``webhook.ping`` delivery for one subscription, to test the endpoint and its signature check."""
    sub = await get(db, principal, sub_id)
    now = _now()
    event_id = uuid.uuid4()
    payload = {
        "id": str(event_id),
        "type": "webhook.ping",
        "created_at": now.isoformat(),
        "data": {"subscription_id": str(sub.id)},
    }
    delivery = WebhookDelivery(
        id=uuid.uuid4(),
        organization_id=sub.organization_id,
        subscription_id=sub.id,
        event_id=event_id,
        event_type="webhook.ping",
        payload=payload,
        payload_sha256=hashlib.sha256(canonical(payload)).hexdigest(),
        attempt=0,
        status="pending",
        next_attempt_at=now,
        created_at=now,
    )
    db.add(delivery)
    await db.flush()
    return delivery
