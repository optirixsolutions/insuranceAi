"""Claim documents: secure upload, reads, human corrections and review actions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BusinessRuleError, ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.integrations.storage.base import ObjectStorage
from app.models.claims import Claim
from app.models.documents import ClaimDocument, DocumentPage, ExtractedField, FieldCorrection
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim, get_writable_claim, scope_claims
from document_processing.classify import DOCUMENT_TYPES
from document_processing.detect import RejectedFileError, inspect, safe_filename
from document_processing.extract import normalise_date, normalise_money
from document_processing.hashing import sha256_hex

logger = get_logger(__name__)
FIELD_TYPES = {
    "policy_number": "identifier",
    "claim_reference": "identifier",
    "invoice_number": "identifier",
    "estimate_number": "identifier",
    "crime_reference": "identifier",
    "vehicle_registration": "identifier",
    "incident_date": "date",
    "invoice_date": "date",
    "admission_date": "date",
    "discharge_date": "date",
    "date_of_birth": "date",
    "total_amount": "money",
    "policyholder_name": "string",
    "patient_name": "string",
    "provider_name": "string",
    "diagnosis": "string",
}


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    filename: str
    document: ClaimDocument | None
    error_code: str | None = None
    error: str | None = None


def _enqueue(org_id: uuid.UUID, document_id: uuid.UUID) -> None:
    from tasks.celery_app import celery_app

    try:
        celery_app.send_task("documents.process", args=[str(org_id), str(document_id)], queue="documents")
    except Exception:  # broker outage: the document stays "queued" and can be reprocessed
        logger.exception("document_enqueue_failed", document_id=str(document_id))


async def _store_one(
    db: AsyncSession,
    settings: Settings,
    storage: ObjectStorage,
    principal: Principal,
    claim: Claim,
    filename: str,
    declared_mime: str,
    data: bytes,
    *,
    supersedes: ClaimDocument | None = None,
) -> UploadOutcome:
    name = safe_filename(filename)
    if len(data) > settings.max_upload_bytes:
        return UploadOutcome(
            name, None, "too_large", f"Files are limited to {settings.max_upload_bytes // 1_048_576} MB"
        )
    try:
        detection = inspect(data, declared_mime)
    except RejectedFileError as exc:
        return UploadOutcome(name, None, exc.code, str(exc))
    digest = sha256_hex(data)
    same = await db.scalar(
        select(ClaimDocument.id).where(
            ClaimDocument.claim_id == claim.id,
            ClaimDocument.sha256 == digest,
            ClaimDocument.deleted_at.is_(None),
        )
    )
    if same is not None:
        return UploadOutcome(
            name, None, "already_uploaded", "This exact file is already attached to the claim"
        )
    doc_id = uuid.uuid4()
    version_no = (supersedes.version_no + 1) if supersedes else 1
    key = (
        f"org/{claim.organization_id}/claims/{claim.id}/docs/{(supersedes.id if supersedes else doc_id)}"
        f"/v{version_no}/{digest}.{detection.extension}"
    )
    await storage.put_bytes(settings.s3_bucket_documents, key, data, detection.mime)
    doc = ClaimDocument(
        id=doc_id,
        organization_id=claim.organization_id,
        claim_id=claim.id,
        version_no=version_no,
        supersedes_id=supersedes.id if supersedes else None,
        original_filename=name,
        declared_mime=declared_mime[:100],
        detected_mime=detection.mime,
        size_bytes=len(data),
        sha256=digest,
        storage_bucket=settings.s3_bucket_documents,
        storage_key=key,
        uploaded_by=principal.user_id,
        is_synthetic=claim.is_synthetic,
    )
    db.add(doc)
    await db.flush()
    return UploadOutcome(name, doc)


async def upload(
    db: AsyncSession,
    settings: Settings,
    storage: ObjectStorage,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    files: list[tuple[str, str, bytes]],
) -> list[UploadOutcome]:
    if not principal.has(P.DOCUMENTS_UPLOAD.value):
        raise ForbiddenError("This action requires the 'documents.upload' permission")
    claim = await get_writable_claim(db, principal, claim_id)
    existing = int(
        await db.scalar(
            select(func.count())
            .select_from(ClaimDocument)
            .where(ClaimDocument.claim_id == claim.id, ClaimDocument.deleted_at.is_(None))
        )
        or 0
    )
    if existing + len(files) > settings.max_documents_per_claim:
        raise BusinessRuleError(f"A claim can hold at most {settings.max_documents_per_claim} documents")
    outcomes = [await _store_one(db, settings, storage, principal, claim, n, m, d) for n, m, d in files]
    for outcome in outcomes:
        await audit_service.record(
            db,
            actor=actor,
            action="document.uploaded" if outcome.document else "document.upload_rejected",
            entity_type="claim_document",
            entity_id=outcome.document.id if outcome.document else None,
            after={
                "claim_id": str(claim.id),
                "filename": outcome.filename,
                "error_code": outcome.error_code,
                "sha256": outcome.document.sha256 if outcome.document else None,
            },
        )
    await db.commit()
    for outcome in outcomes:
        if outcome.document:
            _enqueue(claim.organization_id, outcome.document.id)
    return outcomes


async def upload_version(
    db: AsyncSession,
    settings: Settings,
    storage: ObjectStorage,
    principal: Principal,
    actor: AuditActor,
    document_id: uuid.UUID,
    file: tuple[str, str, bytes],
) -> UploadOutcome:
    old = await get_document(db, principal, document_id)
    claim = await get_writable_claim(db, principal, old.claim_id)
    outcome = await _store_one(db, settings, storage, principal, claim, *file, supersedes=old)
    if outcome.document:
        await audit_service.record(
            db,
            actor=actor,
            action="document.version_uploaded",
            entity_type="claim_document",
            entity_id=outcome.document.id,
            after={"supersedes": str(old.id)},
        )
    await db.commit()
    if outcome.document:
        _enqueue(claim.organization_id, outcome.document.id)
    return outcome


async def get_document(db: AsyncSession, principal: Principal, document_id: uuid.UUID) -> ClaimDocument:
    doc = await db.scalar(
        select(ClaimDocument).where(ClaimDocument.id == document_id, ClaimDocument.deleted_at.is_(None))
    )
    if doc is None:
        raise NotFoundError("Document not found")
    await get_visible_claim(db, principal, doc.claim_id)  # claim visibility governs document visibility
    return doc


async def list_for_claim(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> list[ClaimDocument]:
    claim = await get_visible_claim(db, principal, claim_id)
    newer = select(ClaimDocument.supersedes_id).where(
        ClaimDocument.supersedes_id.is_not(None), ClaimDocument.deleted_at.is_(None)
    )
    rows = await db.scalars(
        select(ClaimDocument)
        .where(
            ClaimDocument.claim_id == claim.id,
            ClaimDocument.deleted_at.is_(None),
            ClaimDocument.id.not_in(newer),
        )
        .order_by(ClaimDocument.created_at)
    )
    return list(rows.all())


async def review_queue(
    db: AsyncSession, principal: Principal, limit: int
) -> list[tuple[ClaimDocument, Claim]]:
    stmt = scope_claims(
        select(ClaimDocument, Claim).join(Claim, Claim.id == ClaimDocument.claim_id), principal
    )
    stmt = stmt.where(
        ClaimDocument.deleted_at.is_(None),
        ClaimDocument.processing_status.in_(["needs_review", "failed", "rejected"]),
    )
    rows = (await db.execute(stmt.order_by(ClaimDocument.created_at.desc()).limit(limit))).all()
    return [(r[0], r[1]) for r in rows]


async def pages(db: AsyncSession, principal: Principal, document_id: uuid.UUID) -> list[DocumentPage]:
    doc = await get_document(db, principal, document_id)
    rows = await db.scalars(
        select(DocumentPage).where(DocumentPage.document_id == doc.id).order_by(DocumentPage.page_number)
    )
    return list(rows.all())


async def fields(db: AsyncSession, principal: Principal, document_id: uuid.UUID) -> list[ExtractedField]:
    doc = await get_document(db, principal, document_id)
    rows = await db.scalars(
        select(ExtractedField)
        .where(ExtractedField.document_id == doc.id, ExtractedField.is_current.is_(True))
        .order_by(ExtractedField.page_number, ExtractedField.field_name)
    )
    return list(rows.all())


async def _writable_doc(db: AsyncSession, principal: Principal, document_id: uuid.UUID) -> ClaimDocument:
    if not principal.has(P.FIELDS_CORRECT.value):
        raise ForbiddenError("This action requires the 'fields.correct' permission")
    doc = await get_document(db, principal, document_id)
    await get_writable_claim(db, principal, doc.claim_id)
    return doc


def _normalise(value_type: str, value: str) -> Any:
    if value_type == "date":
        iso, _ = normalise_date(value)
        if iso is None:
            raise BusinessRuleError("Enter the date as YYYY-MM-DD or DD/MM/YYYY")
        return iso
    if value_type == "money":
        money = normalise_money(value)
        if money is None:
            raise BusinessRuleError("Enter an amount such as 'USD 1,250.00'")
        return money
    if value_type == "identifier":
        return value.strip().upper()
    return " ".join(value.split())


async def _page_text(db: AsyncSession, document_id: uuid.UUID, page_number: int | None) -> str:
    if page_number is None:
        return ""
    return str(
        await db.scalar(
            select(DocumentPage.text).where(
                DocumentPage.document_id == document_id, DocumentPage.page_number == page_number
            )
        )
        or ""
    )


async def correct_field(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    document_id: uuid.UUID,
    field_id: uuid.UUID,
    *,
    value: str,
    reason: str,
) -> ExtractedField:
    doc = await _writable_doc(db, principal, document_id)
    old = await db.scalar(
        select(ExtractedField).where(
            ExtractedField.id == field_id,
            ExtractedField.document_id == doc.id,
            ExtractedField.is_current.is_(True),
        )
    )
    if old is None:
        raise NotFoundError("Field not found or already superseded")
    normalised = _normalise(old.value_type, value)
    grounded = value.strip() in await _page_text(db, doc.id, old.page_number)
    new = ExtractedField(
        organization_id=doc.organization_id,
        document_id=doc.id,
        field_name=old.field_name,
        value_text=value.strip(),
        value_normalized=normalised,
        value_type=old.value_type,
        confidence=1.0,
        page_number=old.page_number,
        evidence_excerpt=old.evidence_excerpt,
        bbox=old.bbox if grounded else None,
        grounding_status="grounded" if grounded else "not_applicable",
        validation_status="corrected",
        source="manual",
        is_current=True,
        verified_by=principal.user_id,
    )
    old.is_current = False
    db.add(new)
    await db.flush()
    db.add(
        FieldCorrection(
            organization_id=doc.organization_id,
            document_id=doc.id,
            previous_field_id=old.id,
            new_field_id=new.id,
            field_name=old.field_name,
            previous_value=old.value_text,
            new_value=new.value_text or "",
            reason=reason,
            corrected_by=principal.user_id,
        )
    )
    # Values may be personal data: the audit event references the correction, the table holds the values.
    await audit_service.record(
        db,
        actor=actor,
        action="document.field_corrected",
        entity_type="claim_document",
        entity_id=doc.id,
        after={
            "field_name": old.field_name,
            "previous_field_id": str(old.id),
            "new_field_id": str(new.id),
            "reason": reason,
        },
    )
    await db.commit()
    return new


async def add_field(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    document_id: uuid.UUID,
    *,
    field_name: str,
    value: str,
    page_number: int | None,
    reason: str,
) -> ExtractedField:
    doc = await _writable_doc(db, principal, document_id)
    if field_name not in FIELD_TYPES:
        raise BusinessRuleError("Unknown field name")
    if page_number is not None and not (1 <= page_number <= (doc.page_count or 0)):
        raise BusinessRuleError("Page number is outside the document")
    existing = await db.scalar(
        select(ExtractedField.id).where(
            ExtractedField.document_id == doc.id,
            ExtractedField.field_name == field_name,
            ExtractedField.is_current.is_(True),
        )
    )
    if existing is not None:
        raise ConflictError("This field already has a value; correct it instead")
    value_type = FIELD_TYPES[field_name]
    grounded = bool(page_number) and value.strip() in await _page_text(db, doc.id, page_number)
    field = ExtractedField(
        organization_id=doc.organization_id,
        document_id=doc.id,
        field_name=field_name,
        value_text=value.strip(),
        value_normalized=_normalise(value_type, value),
        value_type=value_type,
        confidence=1.0,
        page_number=page_number,
        evidence_excerpt=None,
        bbox=None,
        grounding_status="grounded" if grounded else "not_applicable",
        validation_status="corrected",
        source="manual",
        is_current=True,
        verified_by=principal.user_id,
    )
    db.add(field)
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="document.field_added",
        entity_type="claim_document",
        entity_id=doc.id,
        after={"field_name": field_name, "field_id": str(field.id), "reason": reason},
    )
    await db.commit()
    return field


async def verify_field(
    db: AsyncSession, principal: Principal, actor: AuditActor, document_id: uuid.UUID, field_id: uuid.UUID
) -> ExtractedField:
    doc = await _writable_doc(db, principal, document_id)
    field = await db.scalar(
        select(ExtractedField).where(
            ExtractedField.id == field_id,
            ExtractedField.document_id == doc.id,
            ExtractedField.is_current.is_(True),
        )
    )
    if field is None:
        raise NotFoundError("Field not found")
    if field.validation_status != "verified":
        before = field.validation_status
        field.validation_status, field.verified_by = "verified", principal.user_id
        await audit_service.record(
            db,
            actor=actor,
            action="document.field_verified",
            entity_type="claim_document",
            entity_id=doc.id,
            before={"status": before},
            after={"field_name": field.field_name, "field_id": str(field.id)},
        )
        await db.commit()
    return field


async def set_type(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    document_id: uuid.UUID,
    *,
    document_type: str,
    reason: str,
) -> ClaimDocument:
    doc = await _writable_doc(db, principal, document_id)
    if document_type not in DOCUMENT_TYPES:
        raise BusinessRuleError("Unknown document type")
    before = {"type": doc.document_type, "source": doc.classification_source}
    doc.document_type, doc.classification_confidence, doc.classification_source = document_type, 1.0, "manual"
    doc.review_reasons = [
        r for r in doc.review_reasons if r not in ("unclassified", "low_classification_confidence")
    ]
    if doc.processing_status == "needs_review" and not doc.review_reasons:
        doc.processing_status = "processed"
    doc.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="document.reclassified",
        entity_type="claim_document",
        entity_id=doc.id,
        before=before,
        after={"type": document_type, "reason": reason},
    )
    await db.commit()
    return doc


async def reprocess(
    db: AsyncSession, principal: Principal, actor: AuditActor, document_id: uuid.UUID
) -> ClaimDocument:
    doc = await _writable_doc(db, principal, document_id)
    if doc.processing_status in ("queued", "processing"):
        raise ConflictError("The document is already being processed")
    if doc.scan_status == "infected":
        raise BusinessRuleError("Infected files are never processed")
    doc.processing_status = "queued"
    await audit_service.record(
        db, actor=actor, action="document.reprocess_requested", entity_type="claim_document", entity_id=doc.id
    )
    await db.commit()
    _enqueue(doc.organization_id, doc.id)
    return doc


async def delete(
    db: AsyncSession, principal: Principal, actor: AuditActor, document_id: uuid.UUID, reason: str
) -> None:
    doc = await get_document(db, principal, document_id)
    await get_writable_claim(db, principal, doc.claim_id)
    from datetime import UTC, datetime

    doc.deleted_at = datetime.now(UTC)
    await audit_service.record(
        db,
        actor=actor,
        action="document.deleted",
        entity_type="claim_document",
        entity_id=doc.id,
        after={"reason": reason},
    )
    await db.commit()


async def received_types(db: AsyncSession, claim_id: uuid.UUID) -> set[str]:
    rows = await db.scalars(
        select(ClaimDocument.document_type).where(
            ClaimDocument.claim_id == claim_id,
            ClaimDocument.deleted_at.is_(None),
            ClaimDocument.document_type.is_not(None),
            ClaimDocument.processing_status.in_(["processed", "needs_review"]),
        )
    )
    return {t for t in rows.all() if t}
