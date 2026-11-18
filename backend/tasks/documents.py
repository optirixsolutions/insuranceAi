"""Celery tasks for the ``documents`` queue (run by the dedicated document worker)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import get_logger
from app.integrations.antivirus import build_scanner
from app.integrations.storage import build_storage
from app.services.document_processor import RetryableProcessingError, mark_failed, process_document
from document_processing.ocr import OcrEngine, OcrUnavailableError, build_engine
from tasks.celery_app import celery_app

logger = get_logger(__name__)
_engine: OcrEngine | None = None
_engine_checked = False


def _ocr() -> OcrEngine | None:
    """One OCR engine per worker process; None (pages flagged for review) if unavailable."""
    global _engine, _engine_checked
    if not _engine_checked:
        _engine_checked = True
        try:
            _engine = build_engine(get_settings().ocr_engine)
        except OcrUnavailableError as exc:
            logger.error("ocr_unavailable", error=str(exc))
    return _engine


async def _run(organization_id: str, document_id: str, *, final_attempt: bool) -> dict[str, Any]:
    settings = get_settings()
    db = Database.from_settings(settings, application_name="claimos-documents")
    storage = build_storage(settings)
    scanner = build_scanner(settings.av_provider, settings.clamav_host, settings.clamav_port)
    try:
        async with db.sessionmaker() as session:
            try:
                outcome = await process_document(
                    session,
                    settings,
                    storage,
                    scanner,
                    _ocr(),
                    uuid.UUID(organization_id),
                    uuid.UUID(document_id),
                )
                return {"status": outcome.status, "reasons": outcome.reasons}
            except RetryableProcessingError:
                if final_attempt:
                    await session.rollback()
                    await mark_failed(
                        session,
                        uuid.UUID(organization_id),
                        uuid.UUID(document_id),
                        "A dependency was unavailable after several attempts",
                    )
                raise
            except Exception as exc:
                await session.rollback()
                logger.exception("document_processing_error", document_id=document_id)
                await mark_failed(
                    session,
                    uuid.UUID(organization_id),
                    uuid.UUID(document_id),
                    f"Processing error ({type(exc).__name__})",
                )
                return {"status": "failed", "reasons": [type(exc).__name__]}
    finally:
        await db.dispose()


@celery_app.task(name="documents.process", bind=True, max_retries=3, acks_late=True)
def process(self: Any, organization_id: str, document_id: str) -> dict[str, Any]:
    final = self.request.retries >= self.max_retries
    try:
        return asyncio.run(_run(organization_id, document_id, final_attempt=final))
    except RetryableProcessingError as exc:
        raise self.retry(exc=exc, countdown=10 * (2**self.request.retries)) from exc
