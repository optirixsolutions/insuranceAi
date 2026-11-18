"""Rule sets and claim decisions (0008); human reviews and reviewer authorities (0009)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fk(target: str, **kw: Any) -> Any:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, **kw))


class RuleSet(Base):
    __tablename__ = "rule_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    product_id: Mapped[uuid.UUID] = _fk("insurance_products.id", ondelete="CASCADE")
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(String(64))
    change_note: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    published_by: Mapped[uuid.UUID | None] = _fk("users.id")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class ClaimDecision(Base):
    __tablename__ = "claim_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    execution_id: Mapped[uuid.UUID | None] = _fk("workflow_executions.id")
    decision: Mapped[str] = mapped_column(String(30))
    approved_amount: Mapped[Decimal | None] = mapped_column(Numeric(19, 4))
    currency: Mapped[str] = mapped_column(String(3))
    decided_by: Mapped[uuid.UUID | None] = _fk("users.id")
    decision_mode: Mapped[str] = mapped_column(String(10))
    override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(String(1000))
    rule_set_id: Mapped[uuid.UUID | None] = _fk("rule_sets.id")
    gates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    before_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    review_id: Mapped[uuid.UUID | None] = _fk("human_reviews.id")
    second_approver_id: Mapped[uuid.UUID | None] = _fk("users.id")
    reason: Mapped[str | None] = mapped_column(String(1000))
    reason_code: Mapped[str | None] = mapped_column(String(60))
    clause_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HumanReview(Base):
    __tablename__ = "human_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    execution_id: Mapped[uuid.UUID] = _fk("workflow_executions.id")
    interrupt_id: Mapped[str] = mapped_column(String(64))
    recommendation: Mapped[str | None] = mapped_column(String(40))
    payout: Mapped[Decimal | None] = mapped_column(Numeric(19, 4))
    currency: Mapped[str] = mapped_column(String(3))
    fraud_level: Mapped[str | None] = mapped_column(String(20))
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    assigned_to: Mapped[uuid.UUID | None] = _fk("users.id")
    status: Mapped[str] = mapped_column(String(30))
    priority_score: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    four_eyes_required: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class ReviewerAuthority(Base):
    __tablename__ = "reviewer_authorities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    user_id: Mapped[uuid.UUID] = _fk("users.id", ondelete="CASCADE")
    product_id: Mapped[uuid.UUID | None] = _fk("insurance_products.id", ondelete="CASCADE")
    max_approval_amount: Mapped[Decimal] = mapped_column(Numeric(19, 4))
    currency: Mapped[str] = mapped_column(String(3))
    can_reject: Mapped[bool] = mapped_column(Boolean, default=False)
    can_override_fraud: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    granted_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PendingDecision(Base):
    __tablename__ = "pending_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    review_id: Mapped[uuid.UUID] = _fk("human_reviews.id", ondelete="CASCADE")
    decision: Mapped[str] = mapped_column(String(30))
    approved_amount: Mapped[Decimal | None] = mapped_column(Numeric(19, 4))
    reason: Mapped[str] = mapped_column(String(1000))
    reason_code: Mapped[str | None] = mapped_column(String(60))
    override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(String(1000))
    clause_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    first_decider: Mapped[uuid.UUID] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
