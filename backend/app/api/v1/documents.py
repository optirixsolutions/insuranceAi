"""Claim documents: upload, processing results, page previews, field review and corrections."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.api.deps import ActorDep, DbDep, SettingsDep, require
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.integrations.storage import build_storage
from app.integrations.storage.base import ObjectStorage
from app.models.documents import DocumentPage
from app.schemas.documents import (
    DocumentOut,
    FieldCorrection,
    FieldCreate,
    FieldOut,
    PageOut,
    ReviewItem,
    TypeChange,
    UploadResult,
)
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service, document_service
from document_processing.classify import DOCUMENT_TYPES

router = APIRouter(tags=["documents"])
Reader = Annotated[Principal, Depends(require(P.DOCUMENTS_READ))]


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"
    title = "Upload too large"


def get_storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        settings: Settings = request.app.state.settings
        storage = build_storage(settings)
        request.app.state.storage = storage
    return storage


StorageDep = Annotated[ObjectStorage, Depends(get_storage)]


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLargeError(f"'{upload.filename}' is larger than {limit // 1_048_576} MB")
    return data


def _result(outcome: document_service.UploadOutcome) -> UploadResult:
    return UploadResult(
        filename=outcome.filename,
        accepted=outcome.document is not None,
        document=DocumentOut.model_validate(outcome.document) if outcome.document else None,
        error_code=outcome.error_code,
        error=outcome.error,
    )


@router.post(
    "/claims/{claim_id}/documents", response_model=list[UploadResult], status_code=status.HTTP_201_CREATED
)
async def upload_documents(
    claim_id: uuid.UUID,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    settings: SettingsDep,
    storage: StorageDep,
    files: Annotated[list[UploadFile], File(description="One or more PDF, image or DOCX files")],
) -> list[UploadResult]:
    """Each file is checked independently; rejected files are reported without blocking the others."""
    if not files or len(files) > 20:
        raise AppError("Upload between 1 and 20 files at a time")
    payload = [
        (
            f.filename or "document",
            f.content_type or "application/octet-stream",
            await _read_limited(f, settings.max_upload_bytes),
        )
        for f in files
    ]
    outcomes = await document_service.upload(db, settings, storage, principal, actor, claim_id, payload)
    return [_result(o) for o in outcomes]


@router.get("/claims/{claim_id}/documents", response_model=list[DocumentOut])
async def list_documents(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[DocumentOut]:
    return [
        DocumentOut.model_validate(d) for d in await document_service.list_for_claim(db, principal, claim_id)
    ]


@router.get("/documents/review-queue", response_model=list[ReviewItem])
async def review_queue(
    principal: Reader, db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 100
) -> list[ReviewItem]:
    rows = await document_service.review_queue(db, principal, limit)
    return [
        ReviewItem(document=DocumentOut.model_validate(d), claim_reference=c.claim_reference) for d, c in rows
    ]


@router.get("/documents/types", response_model=list[str])
async def document_types(_: Reader) -> list[str]:
    return list(DOCUMENT_TYPES)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, principal: Reader, db: DbDep) -> DocumentOut:
    return DocumentOut.model_validate(await document_service.get_document(db, principal, document_id))


@router.get("/documents/{document_id}/content", response_class=Response)
async def download(
    document_id: uuid.UUID, principal: Reader, actor: ActorDep, db: DbDep, storage: StorageDep
) -> Response:
    """The original file, always as an attachment (never rendered inline by the browser)."""
    doc = await document_service.get_document(db, principal, document_id)
    data = await storage.get_bytes(doc.storage_bucket, doc.storage_key)
    await audit_service.record(
        db, actor=actor, action="document.downloaded", entity_type="claim_document", entity_id=doc.id
    )
    await db.commit()
    return Response(
        data,
        media_type=doc.detected_mime,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.original_filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/documents/{document_id}/pages", response_model=list[PageOut])
async def list_pages(document_id: uuid.UUID, principal: Reader, db: DbDep) -> list[PageOut]:
    out = []
    for page in await document_service.pages(db, principal, document_id):
        item = PageOut.model_validate(page)
        item.has_image = page.image_storage_key is not None
        out.append(item)
    return out


@router.get("/documents/{document_id}/pages/{page_number}/image", response_class=Response)
async def page_image(
    document_id: uuid.UUID, page_number: int, principal: Reader, db: DbDep, storage: StorageDep
) -> Response:
    pages: list[DocumentPage] = await document_service.pages(db, principal, document_id)
    page = next((p for p in pages if p.page_number == page_number), None)
    if page is None or page.image_storage_key is None:
        raise NotFoundError("Page image not found")
    doc = await document_service.get_document(db, principal, document_id)
    data = await storage.get_bytes(doc.storage_bucket, page.image_storage_key)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.get("/documents/{document_id}/fields", response_model=list[FieldOut])
async def list_fields(document_id: uuid.UUID, principal: Reader, db: DbDep) -> list[FieldOut]:
    return [FieldOut.model_validate(f) for f in await document_service.fields(db, principal, document_id)]


@router.post("/documents/{document_id}/fields", response_model=FieldOut, status_code=status.HTTP_201_CREATED)
async def add_field(
    document_id: uuid.UUID, body: FieldCreate, principal: Reader, actor: ActorDep, db: DbDep
) -> FieldOut:
    field = await document_service.add_field(
        db,
        principal,
        actor,
        document_id,
        field_name=body.field_name,
        value=body.value,
        page_number=body.page_number,
        reason=body.reason,
    )
    return FieldOut.model_validate(field)


@router.patch("/documents/{document_id}/fields/{field_id}", response_model=FieldOut)
async def correct_field(
    document_id: uuid.UUID,
    field_id: uuid.UUID,
    body: FieldCorrection,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
) -> FieldOut:
    """Record a human correction. The old value is kept (superseded) and the change is audited."""
    field = await document_service.correct_field(
        db, principal, actor, document_id, field_id, value=body.value, reason=body.reason
    )
    return FieldOut.model_validate(field)


@router.post("/documents/{document_id}/fields/{field_id}/verify", response_model=FieldOut)
async def verify_field(
    document_id: uuid.UUID, field_id: uuid.UUID, principal: Reader, actor: ActorDep, db: DbDep
) -> FieldOut:
    return FieldOut.model_validate(
        await document_service.verify_field(db, principal, actor, document_id, field_id)
    )


@router.patch("/documents/{document_id}/type", response_model=DocumentOut)
async def set_type(
    document_id: uuid.UUID, body: TypeChange, principal: Reader, actor: ActorDep, db: DbDep
) -> DocumentOut:
    doc = await document_service.set_type(
        db, principal, actor, document_id, document_type=body.document_type, reason=body.reason
    )
    return DocumentOut.model_validate(doc)


@router.post("/documents/{document_id}/reprocess", response_model=DocumentOut)
async def reprocess(document_id: uuid.UUID, principal: Reader, actor: ActorDep, db: DbDep) -> DocumentOut:
    return DocumentOut.model_validate(await document_service.reprocess(db, principal, actor, document_id))


@router.post(
    "/documents/{document_id}/versions", response_model=UploadResult, status_code=status.HTTP_201_CREATED
)
async def upload_version(
    document_id: uuid.UUID,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    settings: SettingsDep,
    storage: StorageDep,
    file: Annotated[UploadFile, File()],
) -> UploadResult:
    data = await _read_limited(file, settings.max_upload_bytes)
    outcome = await document_service.upload_version(
        db,
        settings,
        storage,
        principal,
        actor,
        document_id,
        (file.filename or "document", file.content_type or "application/octet-stream", data),
    )
    return _result(outcome)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    reason: Annotated[str, Query(min_length=3, max_length=500)],
) -> None:
    await document_service.delete(db, principal, actor, document_id, reason)
