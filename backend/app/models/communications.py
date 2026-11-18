"""Reports, notifications and webhooks (migration 0010)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fk(target: str, **kw: Any) -> Any:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, **kw))


class ReportVersion(Base):
    __tablename__ = "report_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    decision_id: Mapped[uuid.UUID | None] = _fk("claim_decisions.id")
    version_no: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10))
    template_version: Mapped[str] = mapped_column(String(40))
    storage_key: Mapped[str | None] = mapped_column(String(400))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    evidence_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="CASCADE")
    channel: Mapped[str] = mapped_column(String(10))
    template_key: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(2000))
    link: Mapped[str | None] = mapped_column(String(400))
    claim_id: Mapped[uuid.UUID | None] = _fk("claims.id", ondelete="CASCADE")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    url: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(String(200), default="")
    secret_enc: Mapped[str] = mapped_column(String(400))
    secret_hint: Mapped[str] = mapped_column(String(12))
    event_types: Mapped[list[str]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    subscription_id: Mapped[uuid.UUID] = _fk("webhook_subscriptions.id", ondelete="CASCADE")
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10))
    response_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
