"""Claims, assignments, notes, status history and relations."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ClaimReferenceCounter(Base):
    """Per-organisation, per-year sequence for human-readable claim references."""

    __tablename__ = "claim_reference_counters"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer)


class Claim(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "claims"
    __table_args__ = (
        UniqueConstraint("organization_id", "claim_reference"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"], ["customers.organization_id", "customers.id"]
        ),
        ForeignKeyConstraint(["organization_id", "policy_id"], ["policies.organization_id", "policies.id"]),
        ForeignKeyConstraint(
            ["organization_id", "product_id"], ["insurance_products.organization_id", "insurance_products.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "policy_version_id"],
            ["policy_versions.organization_id", "policy_versions.id"],
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    claim_reference: Mapped[str] = mapped_column(String(30))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(30), server_default="SUBMITTED")
    category: Mapped[str] = mapped_column(String(40))
    priority: Mapped[str] = mapped_column(String(10), server_default="normal")
    incident_date: Mapped[date] = mapped_column(Date)
    incident_time: Mapped[time | None] = mapped_column(Time)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    incident_location: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(Text)
    claimed_amount: Mapped[Decimal] = mapped_column(Numeric(19, 4))
    currency: Mapped[str] = mapped_column(String(3))
    channel: Mapped[str] = mapped_column(String(20), server_default="staff")
    intake_findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


def _claim_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "claim_id"], ["claims.organization_id", "claims.id"], ondelete="CASCADE"
    )


class ClaimAssignment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "claim_assignments"
    __table_args__ = (
        _claim_fk(),
        ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    role_on_claim: Mapped[str] = mapped_column(String(20))
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    unassigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "claim_notes"
    __table_args__ = (_claim_fk(),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    visibility: Mapped[str] = mapped_column(String(20), server_default="internal")
    body: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimStatusChange(UUIDPrimaryKeyMixin, Base):
    """Append-only history of status transitions (drives the activity timeline)."""

    __tablename__ = "claim_status_history"
    __table_args__ = (_claim_fk(),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(20))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ClaimRelation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "claim_relations"
    __table_args__ = (
        _claim_fk(),
        ForeignKeyConstraint(
            ["organization_id", "related_claim_id"],
            ["claims.organization_id", "claims.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("claim_id", "related_claim_id", "relation"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    related_claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    relation: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
