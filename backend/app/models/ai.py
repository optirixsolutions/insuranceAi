"""Model registry, per-agent model configuration and model invocation records.

All tables are organisation-scoped: each organisation decides which models it trusts and
pays for. Invocation records hold usage and outcome metadata only; prompts and completions
are never stored (only SHA-256 digests, for correlation).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ModelRegistryEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_registry"
    __table_args__ = (UniqueConstraint("organization_id", "slug"), UniqueConstraint("organization_id", "id"))

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), server_default="openrouter")
    slug: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    is_free: Mapped[bool] = mapped_column(Boolean)
    context_length: Mapped[int | None] = mapped_column(Integer)
    max_completion_tokens: Mapped[int | None] = mapped_column(Integer)
    input_modalities: Mapped[list[str]] = mapped_column(ARRAY(String(20)), server_default=sql_text("'{}'"))
    # Declared by the catalogue: {"json_mode": bool, "json_schema": bool, "tools": bool, "vision": bool}
    declared_capabilities: Mapped[dict[str, bool]] = mapped_column(
        JSONB, server_default=sql_text("'{}'::jsonb")
    )
    # Verified by probes: {"json_mode": {"ok": bool, "latency_ms": int, "detail": str, "tested_at": iso}}
    verified_capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sql_text("'{}'::jsonb")
    )
    probe_status: Mapped[str] = mapped_column(String(20), server_default="never")
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prompt_price_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    completion_price_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    data_policy: Mapped[str] = mapped_column(String(30), server_default="unknown")
    data_policy_note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    in_catalog: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    requests_per_minute: Mapped[int] = mapped_column(Integer, server_default="15")
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class AgentConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("organization_id", "agent_key"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    agent_key: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    primary_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    fallback_model_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=sql_text("'{}'")
    )
    required_capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), server_default=sql_text("'{}'")
    )
    confidence_threshold: Mapped[float] = mapped_column(Float, server_default="0.85")
    max_retries: Mapped[int] = mapped_column(Integer, server_default="2")
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default="60")
    max_output_tokens: Mapped[int] = mapped_column(Integer, server_default="2000")
    temperature: Mapped[float] = mapped_column(Float, server_default="0")
    prompt_version: Mapped[str] = mapped_column(String(20), server_default="v1")  # Phase 12
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class ModelInvocation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "model_invocations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    agent_key: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(40))
    model_slug: Mapped[str] = mapped_column(String(200))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    error_code: Mapped[str | None] = mapped_column(String(60))
    latency_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), server_default="0")
    request_sha256: Mapped[str] = mapped_column(String(64))
    data_classification: Mapped[str] = mapped_column(String(20))
    request_id: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(120))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
