"""Evaluation runs and agent feedback (migration 0011). Claim embeddings are read and written with raw
SQL (``pgvector``), see ``app.services.similarity``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fk(target: str, **kw: Any) -> Any:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, **kw))


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    agent_key: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10))
    prompt_version: Mapped[str] = mapped_column(String(20))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[uuid.UUID | None] = _fk("model_registry.id", ondelete="SET NULL")
    model_slug: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10))
    cases: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    regressions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentFeedback(Base):
    __tablename__ = "agent_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id", ondelete="CASCADE")
    claim_id: Mapped[uuid.UUID] = _fk("claims.id", ondelete="CASCADE")
    agent_run_id: Mapped[uuid.UUID] = _fk("agent_runs.id", ondelete="CASCADE")
    agent_key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(1000))
    created_by: Mapped[uuid.UUID | None] = _fk("users.id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
