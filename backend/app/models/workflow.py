"""Workflow executions, their event outbox and checkpoint index (migration 0007)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fk(target: str, **kw: Any) -> Any:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, **kw))


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    graph_name: Mapped[str] = mapped_column(String(60))
    graph_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    trigger: Mapped[str] = mapped_column(String(20))
    current_node: Mapped[str | None] = mapped_column(String(60))
    interrupt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    progress: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0"))
    iteration_count: Mapped[int] = mapped_column(Integer, default=0)
    reassessment_count: Mapped[int] = mapped_column(Integer, default=0)
    more_info_count: Mapped[int] = mapped_column(Integer, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, default=200_000)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    started_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_detail: Mapped[str | None] = mapped_column(String(1000))
    celery_task_id: Mapped[str | None] = mapped_column(String(80))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    execution_id: Mapped[uuid.UUID] = _fk("workflow_executions.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    seq: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(40))
    node: Mapped[str | None] = mapped_column(String(60))
    agent_key: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    execution_id: Mapped[uuid.UUID] = _fk("workflow_executions.id", ondelete="CASCADE")
    checkpoint_id: Mapped[str] = mapped_column(String(80))
    checkpoint_ns: Mapped[str] = mapped_column(String(200), default="")
    node: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
