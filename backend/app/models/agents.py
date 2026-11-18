"""Agent runs, validated agent outputs and prompt versions (migration 0006)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fk(target: str, **kw: Any) -> Any:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, **kw))


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_key: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(20))
    template_path: Mapped[str] = mapped_column(String(200))
    content_sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_key: Mapped[str] = mapped_column(String(40))
    node: Mapped[str] = mapped_column(String(60))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    model_slug: Mapped[str | None] = mapped_column(String(200))
    prompt_version_id: Mapped[uuid.UUID | None] = _fk("prompt_versions.id")
    rule_set_version: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    used_llm: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_note: Mapped[str | None] = mapped_column(String(500))
    escalate: Mapped[bool] = mapped_column(Boolean, default=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    missing: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_detail: Mapped[str | None] = mapped_column(String(1000))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    triggered_by: Mapped[uuid.UUID | None] = _fk("users.id")


class AgentOutput(Base):
    __tablename__ = "agent_outputs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    agent_run_id: Mapped[uuid.UUID] = _fk("agent_runs.id", ondelete="CASCADE")
    schema_name: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[str] = mapped_column(String(20))
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    validation_errors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    output_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
