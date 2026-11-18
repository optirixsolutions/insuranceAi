"""Reports, notifications, webhooks and messaging integrations (05 §2.9, §2.12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.api.deps import ActorDep, DbDep, PrincipalDep, SettingsDep, require
from app.models.communications import Notification, ReportVersion, WebhookDelivery, WebhookSubscription
from app.security.permissions import P
from app.security.principal import Principal
from app.services import idempotency, notification_service, report_service, webhook_service

router = APIRouter(tags=["communications"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Reporter = Annotated[Principal, Depends(require(P.REPORTS_GENERATE))]
OrgAdmin = Annotated[Principal, Depends(require(P.ORG_MANAGE))]


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_id: uuid.UUID
    decision_id: uuid.UUID | None
    version_no: int
    format: str
    status: str
    template_version: str
    sha256: str | None
    size_bytes: int | None
    evidence_count: int | None
    error: str | None
    requested_by: uuid.UUID | None
    created_at: datetime
    generated_at: datetime | None


class RecentReport(ReportOut):
    claim_reference: str


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["pdf", "json"] = "pdf"


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    template_key: str
    title: str
    body: str
    link: str | None
    claim_id: uuid.UUID | None
    created_at: datetime
    read_at: datetime | None


class Inbox(BaseModel):
    unread: int
    items: list[NotificationOut]


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: Annotated[str, Field(min_length=8, max_length=500)]
    event_types: Annotated[list[str], Field(min_length=1, max_length=20)]
    description: Annotated[str, Field(max_length=200)] = ""


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    description: str
    secret_hint: str
    event_types: list[str]
    active: bool
    created_at: datetime
    disabled_at: datetime | None


class WebhookCreated(WebhookOut):
    secret: str  # shown once


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    attempt: int
    status: str
    response_code: int | None
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    delivered_at: datetime | None


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["email", "sms"]
    to: Annotated[str | None, Field(max_length=40, pattern=r"^\+?[0-9 ]{6,20}$")] = None  # SMS number


# --- reports -------------------------------------------------------------------------------------


@router.post("/claims/{claim_id}/reports", response_model=ReportOut, status_code=status.HTTP_202_ACCEPTED)
async def request_report(
    claim_id: uuid.UUID,
    body: ReportRequest,
    request: Request,
    principal: Reporter,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> ReportOut | JSONResponse:
    """Queue a report version; generation runs on the ``reports`` queue. Requires an Idempotency-Key."""
    key = idempotency.validate_key(idempotency_key)
    digest = idempotency.request_hash(body.model_dump(mode="json"))
    path = request.url.path
    replay = await idempotency.find_replay(db, principal, key, "POST", path, digest)
    if replay is not None:
        return JSONResponse(replay[1], status_code=replay[0], headers={"Idempotent-Replayed": "true"})
    report = await report_service.request(db, principal, actor, claim_id, body.format)
    out = ReportOut.model_validate(report)
    idempotency.remember(db, principal, key, "POST", path, digest, 202, out.model_dump(mode="json"))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        replay = await idempotency.find_replay(db, principal, key, "POST", path, digest)
        if replay is None:
            raise
        return JSONResponse(replay[1], status_code=replay[0], headers={"Idempotent-Replayed": "true"})
    report_service.enqueue(report)
    return out


@router.get("/claims/{claim_id}/reports", response_model=list[ReportOut])
async def claim_reports(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[ReportVersion]:
    return await report_service.list_for_claim(db, principal, claim_id)


@router.get("/reports", response_model=list[RecentReport])
async def recent_reports(
    principal: Reader, db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[RecentReport]:
    rows = await report_service.recent(db, principal, limit)
    return [RecentReport(**ReportOut.model_validate(r).model_dump(), claim_reference=ref) for r, ref in rows]


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: uuid.UUID, principal: Reader, actor: ActorDep, db: DbDep, settings: SettingsDep
) -> Response:
    report, claim, data = await report_service.download(db, principal, actor, settings, report_id)
    await db.commit()  # the audit event
    name = f"{claim.claim_reference}-report-v{report.version_no}.{report.format}"
    return Response(
        content=data,
        media_type=report_service.CONTENT_TYPES[report.format],
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-SHA256": report.sha256 or "",
            "Cache-Control": "no-store",
        },
    )


# --- notifications -------------------------------------------------------------------------------


@router.get("/notifications", response_model=Inbox)
async def notifications(
    principal: PrincipalDep,
    db: DbDep,
    unread: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Inbox:
    rows, count = await notification_service.inbox(db, principal, unread_only=unread, limit=limit)
    return Inbox(unread=count, items=[NotificationOut.model_validate(n) for n in rows])


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def read_notification(notification_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> Notification:
    n = await notification_service.mark_read(db, principal, notification_id)
    await db.commit()
    return n


@router.post("/notifications/read-all")
async def read_all(principal: PrincipalDep, db: DbDep) -> dict[str, int]:
    changed = await notification_service.mark_all_read(db, principal)
    await db.commit()
    return {"marked_read": changed}


# --- webhooks ------------------------------------------------------------------------------------


@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(principal: OrgAdmin, db: DbDep) -> list[WebhookSubscription]:
    return await webhook_service.list_subscriptions(db, principal)


@router.get("/webhooks/event-types")
async def webhook_event_types(principal: OrgAdmin) -> list[str]:
    return list(webhook_service.EVENT_TYPES)


@router.post("/webhooks", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookIn, principal: OrgAdmin, actor: ActorDep, db: DbDep, settings: SettingsDep
) -> WebhookCreated:
    sub, secret = await webhook_service.create(
        db,
        principal,
        actor,
        settings,
        url=body.url,
        event_types=body.event_types,
        description=body.description,
    )
    await db.commit()
    return WebhookCreated(**WebhookOut.model_validate(sub).model_dump(), secret=secret)


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: uuid.UUID, principal: OrgAdmin, actor: ActorDep, db: DbDep) -> Response:
    await webhook_service.disable(db, principal, actor, webhook_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/webhooks/{webhook_id}/deliveries", response_model=list[DeliveryOut])
async def webhook_deliveries(webhook_id: uuid.UUID, principal: OrgAdmin, db: DbDep) -> list[WebhookDelivery]:
    return await webhook_service.deliveries(db, principal, webhook_id)


@router.post("/webhooks/{webhook_id}/ping", response_model=DeliveryOut, status_code=status.HTTP_202_ACCEPTED)
async def ping_webhook(webhook_id: uuid.UUID, principal: OrgAdmin, db: DbDep) -> WebhookDelivery:
    """Queue a ``webhook.ping`` event for this subscription only (delivered by the dispatcher)."""
    delivery = await webhook_service.ping(db, principal, webhook_id)
    await db.commit()
    return delivery


# --- integrations --------------------------------------------------------------------------------


@router.get("/integrations/status")
async def integrations_status(principal: OrgAdmin, db: DbDep, settings: SettingsDep) -> dict[str, Any]:
    subs = await webhook_service.list_subscriptions(db, principal)
    return {
        "email": {
            "provider": settings.email_provider,
            "from": settings.smtp_from,
            "host": settings.smtp_host,
        },
        "sms": {"provider": settings.sms_provider, "sends_real_messages": False},
        "webhooks": {"active": sum(1 for s in subs if s.active), "total": len(subs)},
        "encryption_key_configured": settings.data_encryption_key is not None,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.post("/integrations/test-message")
async def test_message(
    body: TestMessage, principal: OrgAdmin, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """Send a test email to yourself, or a test SMS through the configured (log) adapter."""
    n = await notification_service.test_message(db, principal, settings, body.channel, body.to)
    await db.commit()
    return {"status": n.status, "provider": n.provider, "message_id": n.provider_message_id, "error": n.error}
