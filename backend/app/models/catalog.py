"""Insurance products, policies (versioned) and customers."""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

MONEY = Numeric(19, 4)


def _org_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


class InsuranceProduct(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "insurance_products"
    __table_args__ = (UniqueConstraint("organization_id", "code"), UniqueConstraint("organization_id", "id"))

    organization_id: Mapped[uuid.UUID] = _org_fk()
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    line: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class DocumentRequirement(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_requirements"
    __table_args__ = (
        UniqueConstraint("product_id", "document_type"),
        ForeignKeyConstraint(
            ["organization_id", "product_id"],
            ["insurance_products.organization_id", "insurance_products.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_type: Mapped[str] = mapped_column(String(40))
    mandatory: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    description: Mapped[str] = mapped_column(String(300), server_default="")


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("organization_id", "id"),)

    organization_id: Mapped[uuid.UUID] = _org_fk()
    external_ref: Mapped[str | None] = mapped_column(String(80))
    customer_type: Mapped[str] = mapped_column(String(20), server_default="individual")
    full_name: Mapped[str] = mapped_column(String(200))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class Policy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "policy_number"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"], ["customers.organization_id", "customers.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "product_id"], ["insurance_products.organization_id", "insurance_products.id"]
        ),
    )

    organization_id: Mapped[uuid.UUID] = _org_fk()
    policy_number: Mapped[str] = mapped_column(String(60))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    source: Mapped[str] = mapped_column(String(20), server_default="manual")
    external_ref: Mapped[str | None] = mapped_column(String(80))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class PolicyVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Terms valid for [effective_from, effective_to). Immutable once published."""

    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version_no"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "policy_id"], ["policies.organization_id", "policies.id"], ondelete="CASCADE"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    sum_insured: Mapped[Decimal] = mapped_column(MONEY)
    deductible_amount: Mapped[Decimal] = mapped_column(MONEY)
    annual_aggregate_limit: Mapped[Decimal | None] = mapped_column(MONEY)
    waiting_period_days: Mapped[int] = mapped_column(Integer, server_default="0")
    endorsements: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), server_default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class PolicyClause(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "policy_clauses"
    __table_args__ = (
        UniqueConstraint("policy_version_id", "clause_ref"),
        ForeignKeyConstraint(
            ["organization_id", "policy_version_id"],
            ["policy_versions.organization_id", "policy_versions.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    clause_ref: Mapped[str] = mapped_column(String(40))
    section: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)


class PolicyCoverage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "policy_coverages"
    __table_args__ = (
        UniqueConstraint("policy_version_id", "coverage_code"),
        ForeignKeyConstraint(
            ["organization_id", "policy_version_id"],
            ["policy_versions.organization_id", "policy_versions.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    coverage_code: Mapped[str] = mapped_column(String(40))
    perils: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    limit_amount: Mapped[Decimal] = mapped_column(MONEY)
    deductible_override: Mapped[Decimal | None] = mapped_column(MONEY)
    co_pay_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    clause_ref: Mapped[str] = mapped_column(String(40))


class PolicyExclusion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "policy_exclusions"
    __table_args__ = (
        UniqueConstraint("policy_version_id", "exclusion_code"),
        ForeignKeyConstraint(
            ["organization_id", "policy_version_id"],
            ["policy_versions.organization_id", "policy_versions.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    exclusion_code: Mapped[str] = mapped_column(String(40))
    applies_to: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    description: Mapped[str] = mapped_column(Text)
    clause_ref: Mapped[str] = mapped_column(String(40))
