"""Claim documents, rendered pages, extractions, extracted fields and corrections.

Binary content never lives in PostgreSQL (ADR-012): rows hold object-storage keys,
hashes and metadata. Every extracted value points at a page and the text it came from.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _doc_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "document_id"],
        ["claim_documents.organization_id", "claim_documents.id"],
        ondelete="CASCADE",
    )


class ClaimDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "claim_documents"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "claim_id"], ["claims.organization_id", "claims.id"], ondelete="CASCADE"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    version_no: Mapped[int] = mapped_column(Integer, server_default="1")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    original_filename: Mapped[str] = mapped_column(String(255))
    declared_mime: Mapped[str] = mapped_column(String(100))
    detected_mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(16))
    text_simhash: Mapped[str | None] = mapped_column(String(16))
    storage_bucket: Mapped[str] = mapped_column(String(100))
    storage_key: Mapped[str] = mapped_column(String(400))
    scan_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    processing_status: Mapped[str] = mapped_column(String(20), server_default="queued")
    document_type: Mapped[str | None] = mapped_column(String(40))
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    classification_source: Mapped[str | None] = mapped_column(String(20))
    page_count: Mapped[int | None] = mapped_column(Integer)
    quality_score: Mapped[float | None] = mapped_column(Float)
    review_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(60)), server_default=sql_text("'{}'"))
    duplicates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    error_detail: Mapped[str | None] = mapped_column(String(300))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class DocumentPage(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number"), _doc_fk())

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    text_source: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float] = mapped_column(Float)
    quality_flags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), server_default=sql_text("'{}'"))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    rotation_applied: Mapped[int] = mapped_column(Integer, server_default="0")
    image_storage_key: Mapped[str | None] = mapped_column(String(400))
    # Word boxes normalised to 0..1 page coordinates: [{"t": text, "b": [x0, y0, x1, y1], "c": conf}]
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DocumentExtraction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_extractions"
    __table_args__ = (_doc_fk(),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    method: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ExtractedField(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "extracted_fields"
    __table_args__ = (_doc_fk(),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    field_name: Mapped[str] = mapped_column(String(60))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_normalized: Mapped[Any] = mapped_column(JSONB, nullable=True)
    value_type: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float)
    page_number: Mapped[int | None] = mapped_column(Integer)
    evidence_excerpt: Mapped[str | None] = mapped_column(String(500))
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)
    grounding_status: Mapped[str] = mapped_column(String(20))
    validation_status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FieldCorrection(UUIDPrimaryKeyMixin, Base):
    """Append-only record of a human correction to an extracted value."""

    __tablename__ = "field_corrections"
    __table_args__ = (_doc_fk(),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    previous_field_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    new_field_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field_name: Mapped[str] = mapped_column(String(60))
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(500))
    corrected_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
