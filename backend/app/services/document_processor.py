"""Background processing of one claim document (runs in the ``documents`` worker).

Steps: malware scan -> page extraction (text layer / OCR) -> page previews -> classification
-> field extraction with evidence -> duplicate detection -> review routing. Every run is
recorded; failures leave a clear status and reason rather than partial silent results.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from typing import Any

import anyio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.tenancy import bind_tenant
from app.integrations.antivirus import Scanner
from app.integrations.storage.base import ObjectStorage
from app.models.claims import Claim
from app.models.documents import ClaimDocument, DocumentExtraction, DocumentPage, ExtractedField
from app.services import audit_service
from app.services.audit_service import AuditActor
from document_processing import detect
from document_processing.classify import classify
from document_processing.detect import RejectedFileError
from document_processing.extract import extract_fields
from document_processing.hashing import (
    PHASH_DUPLICATE_DISTANCE,
    SIMHASH_DUPLICATE_DISTANCE,
    dhash,
    hamming,
    simhash,
)
from document_processing.ocr import OcrEngine
from document_processing.pages import PageContent, extract_pages

logger = get_logger(__name__)
SYSTEM = AuditActor("system", None)
UNREADABLE_SCORE = 0.35
LOW_PAGE_SCORE = 0.55
CLASSIFICATION_THRESHOLD = 0.7
EXTRACTION_METHOD = "rules_v1"


class RetryableProcessingError(RuntimeError):
    """A dependency (storage, antivirus) failed; the task should be retried."""


@dataclass(frozen=True, slots=True)
class ProcessingOutcome:
    status: str
    reasons: list[str]


async def _duplicates(db: AsyncSession, doc: ClaimDocument) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(
                ClaimDocument.id,
                ClaimDocument.claim_id,
                ClaimDocument.sha256,
                ClaimDocument.perceptual_hash,
                ClaimDocument.text_simhash,
                Claim.claim_reference,
            )
            .join(Claim, Claim.id == ClaimDocument.claim_id)
            .where(
                ClaimDocument.organization_id == doc.organization_id,
                ClaimDocument.id != doc.id,
                ClaimDocument.deleted_at.is_(None),
                ClaimDocument.supersedes_id.is_distinct_from(doc.id),
                ClaimDocument.id != (doc.supersedes_id or uuid.UUID(int=0)),
            )
            .order_by(ClaimDocument.created_at.desc())
            .limit(5000)
        )
    ).all()
    matches: list[dict[str, Any]] = []
    for other_id, claim_id, sha, phash, shash, reference in rows:
        method, distance = None, None
        if sha == doc.sha256:
            method, distance = "exact", 0
        elif (
            doc.perceptual_hash and phash and hamming(doc.perceptual_hash, phash) <= PHASH_DUPLICATE_DISTANCE
        ):
            method, distance = "image_similarity", hamming(doc.perceptual_hash, phash)
        elif doc.text_simhash and shash and hamming(doc.text_simhash, shash) <= SIMHASH_DUPLICATE_DISTANCE:
            method, distance = "text_similarity", hamming(doc.text_simhash, shash)
        if method:
            matches.append(
                {
                    "document_id": str(other_id),
                    "claim_id": str(claim_id),
                    "claim_reference": reference,
                    "method": method,
                    "distance": distance,
                    "same_claim": claim_id == doc.claim_id,
                }
            )
    return matches[:20]


async def _store_pages(
    db: AsyncSession, storage: ObjectStorage, doc: ClaimDocument, pages: list[PageContent]
) -> None:
    await db.execute(delete(DocumentPage).where(DocumentPage.document_id == doc.id))
    base = doc.storage_key.rsplit("/", 1)[0]
    for page in pages:
        key = None
        if page.preview_png:
            key = f"{base}/pages/{page.page_number}.png"
            await storage.put_bytes(doc.storage_bucket, key, page.preview_png, "image/png")
        db.add(
            DocumentPage(
                organization_id=doc.organization_id,
                document_id=doc.id,
                page_number=page.page_number,
                text_source=page.text_source,
                text=page.text,
                ocr_confidence=page.ocr_confidence,
                quality_score=page.score,
                quality_flags=page.flags,
                width=page.width,
                height=page.height,
                rotation_applied=page.rotation_applied,
                image_storage_key=key,
                words=[
                    {"t": w.text, "b": [round(v, 4) for v in w.box], "c": round(w.confidence, 3)}
                    for w in page.words
                ],
            )
        )


async def _store_fields(
    db: AsyncSession, settings: Settings, doc: ClaimDocument, pages: list[PageContent]
) -> int:
    started = time.perf_counter()
    previous = int(
        await db.scalar(
            select(DocumentExtraction.attempt)
            .where(DocumentExtraction.document_id == doc.id)
            .order_by(DocumentExtraction.attempt.desc())
            .limit(1)
        )
        or 0
    )
    extraction = DocumentExtraction(
        organization_id=doc.organization_id,
        document_id=doc.id,
        method=EXTRACTION_METHOD,
        status="completed",
        attempt=previous + 1,
    )
    db.add(extraction)
    await db.flush()
    manual = set(
        (
            await db.scalars(
                select(ExtractedField.field_name).where(
                    ExtractedField.document_id == doc.id,
                    ExtractedField.is_current.is_(True),
                    ExtractedField.source == "manual",
                )
            )
        ).all()
    )
    # Earlier machine extractions are superseded; human corrections stay authoritative.
    await db.execute(
        update(ExtractedField)
        .where(ExtractedField.document_id == doc.id, ExtractedField.source == "rules")
        .values(is_current=False)
    )
    candidates = extract_fields([(p.page_number, p.text, p.words, p.ocr_confidence) for p in pages])
    low = 0
    for c in candidates:
        if c.field_name in manual:
            continue
        status = "unverified" if c.confidence >= settings.field_confidence_threshold else "low_confidence"
        low += status == "low_confidence"
        db.add(
            ExtractedField(
                organization_id=doc.organization_id,
                document_id=doc.id,
                extraction_id=extraction.id,
                field_name=c.field_name,
                value_text=c.value_text,
                value_normalized=c.value_normalized,
                value_type=c.value_type,
                confidence=c.confidence,
                page_number=c.page_number,
                evidence_excerpt=c.evidence_excerpt,
                bbox=c.bbox,
                grounding_status="grounded",
                validation_status=status,
                source="rules",
                is_current=True,
            )
        )
    extraction.duration_ms = int((time.perf_counter() - started) * 1000)
    return low


async def process_document(
    db: AsyncSession,
    settings: Settings,
    storage: ObjectStorage,
    scanner: Scanner,
    ocr: OcrEngine | None,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
) -> ProcessingOutcome:
    await bind_tenant(db, organization_id)
    doc = await db.scalar(select(ClaimDocument).where(ClaimDocument.id == document_id))
    if doc is None or doc.deleted_at is not None:
        return ProcessingOutcome("skipped", ["document_missing_or_deleted"])
    doc.processing_status, doc.attempts, doc.error_detail = "processing", doc.attempts + 1, None
    await db.commit()

    try:
        data = await storage.get_bytes(doc.storage_bucket, doc.storage_key)
    except Exception as exc:
        raise RetryableProcessingError(f"storage read failed ({type(exc).__name__})") from exc

    scan = await anyio.to_thread.run_sync(scanner.scan, data)
    doc.scan_status = scan.status
    if scan.status == "error":
        raise RetryableProcessingError(scan.detail or "antivirus error")
    if scan.status == "infected":
        doc.processing_status, doc.error_detail = "rejected", f"Malware detected: {scan.signature}"
        await audit_service.record(
            db,
            actor=SYSTEM,
            action="document.infected",
            entity_type="claim_document",
            entity_id=doc.id,
            after={"signature": scan.signature},
        )
        await db.commit()
        return ProcessingOutcome("rejected", ["malware"])

    try:
        pages = await anyio.to_thread.run_sync(
            lambda: extract_pages(data, doc.detected_mime, ocr, max_pages=settings.max_pages_per_document)
        )
    except RejectedFileError as exc:
        doc.processing_status, doc.error_detail = "rejected", str(exc)[:300]
        await db.commit()
        return ProcessingOutcome("rejected", [exc.code])

    await _store_pages(db, storage, doc, pages)
    full_text = "\n".join(p.text for p in pages)
    is_image = doc.detected_mime in detect.IMAGE_TYPES
    if doc.classification_source != "manual":
        result = classify(full_text, is_image=is_image)
        doc.document_type, doc.classification_confidence = result.document_type, result.confidence
        doc.classification_source = "rules" if result.document_type else None
    low_fields = await _store_fields(db, settings, doc, pages)

    first_image = next((p.image_for_hash for p in pages if p.image_for_hash is not None), None)
    doc.perceptual_hash = (
        dhash(first_image) if first_image is not None and (is_image or not full_text.strip()) else None
    )
    doc.text_simhash = simhash(full_text)
    doc.duplicates = await _duplicates(db, doc)
    doc.page_count = len(pages)
    doc.quality_score = round(mean(p.score for p in pages), 3) if pages else 0.0

    reasons: list[str] = []
    if doc.quality_score < UNREADABLE_SCORE and doc.document_type != "photo_evidence":
        reasons.append("unreadable")
    elif any(p.score < LOW_PAGE_SCORE for p in pages) and doc.document_type != "photo_evidence":
        reasons.append("low_quality_pages")
    if any("ocr_unavailable" in p.flags for p in pages):
        reasons.append("ocr_unavailable")
    if doc.document_type is None:
        reasons.append("unclassified")
    elif (doc.classification_confidence or 0) < CLASSIFICATION_THRESHOLD:
        reasons.append("low_classification_confidence")
    if low_fields:
        reasons.append("low_confidence_fields")
    if doc.duplicates:
        reasons.append("possible_duplicate")
    doc.review_reasons = reasons
    doc.processing_status = "needs_review" if reasons else "processed"
    doc.processed_at = datetime.now(UTC)
    doc.row_version += 1
    await audit_service.record(
        db,
        actor=SYSTEM,
        action="document.processed",
        entity_type="claim_document",
        entity_id=doc.id,
        after={
            "status": doc.processing_status,
            "type": doc.document_type,
            "pages": doc.page_count,
            "reasons": reasons,
        },
    )
    await db.commit()
    logger.info(
        "document_processed",
        document_id=str(doc.id),
        status=doc.processing_status,
        pages=doc.page_count,
        reasons=reasons,
    )
    return ProcessingOutcome(doc.processing_status, reasons)


async def mark_failed(
    db: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID, detail: str
) -> None:
    await bind_tenant(db, organization_id)
    doc = await db.scalar(select(ClaimDocument).where(ClaimDocument.id == document_id))
    if doc is None:
        return
    doc.processing_status, doc.error_detail = "failed", detail[:300]
    await audit_service.record(
        db,
        actor=SYSTEM,
        action="document.processing_failed",
        entity_type="claim_document",
        entity_id=doc.id,
        after={"detail": detail[:300]},
    )
    await db.commit()
