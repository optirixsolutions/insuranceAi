from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_id: uuid.UUID
    version_no: int
    supersedes_id: uuid.UUID | None
    original_filename: str
    detected_mime: str
    size_bytes: int
    sha256: str
    scan_status: str
    processing_status: str
    document_type: str | None
    classification_confidence: float | None
    classification_source: str | None
    page_count: int | None
    quality_score: float | None
    review_reasons: list[str]
    duplicates: list[dict[str, Any]]
    error_detail: str | None
    processed_at: datetime | None
    created_at: datetime
    row_version: int


class UploadResult(BaseModel):
    filename: str
    accepted: bool
    document: DocumentOut | None = None
    error_code: str | None = None
    error: str | None = None


class ReviewItem(BaseModel):
    document: DocumentOut
    claim_reference: str


class WordOut(BaseModel):
    t: str
    b: list[float]
    c: float


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    page_number: int
    text_source: str
    text: str
    ocr_confidence: float | None
    quality_score: float
    quality_flags: list[str]
    width: int | None
    height: int | None
    rotation_applied: int
    has_image: bool = False
    words: list[WordOut]


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    field_name: str
    value_text: str | None
    value_normalized: Any
    value_type: str
    confidence: float
    page_number: int | None
    evidence_excerpt: str | None
    bbox: list[float] | None
    grounding_status: str
    validation_status: str
    source: str
    created_at: datetime


class FieldCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    reason: Reason


class FieldCreate(FieldCorrection):
    field_name: Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,60}$")]
    page_number: Annotated[int, Field(ge=1)] | None = None


class TypeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_type: Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,40}$")]
    reason: Reason
