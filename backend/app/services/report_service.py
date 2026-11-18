"""Claim reports (05 §2.9): request (API) -> generate (``reports`` queue) -> audited download.

A request inserts a ``queued`` report version (numbered per claim and format) and is idempotent at the
API. The worker builds the JSON content (``report_builder``), renders PDF when asked (``report_pdf``),
stores the bytes in the reports bucket with their SHA-256 and marks the version ``ready`` (immutable
from then on, by trigger). A download re-checks the hash before serving and is audited.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import bind_tenant
from app.integrations.storage import build_storage
from app.models.claims import Claim
from app.models.communications import ReportVersion
from app.models.rules import ClaimDecision
from app.security.principal import Principal
from app.services import audit_service, events, report_builder, report_pdf
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim

CONTENT_TYPES = {"pdf": "application/pdf", "json": "application/json"}


def _now() -> datetime:
    return datetime.now(UTC)


async def request(
    db: AsyncSession, principal: Principal, actor: AuditActor, claim_id: uuid.UUID, fmt: str
) -> ReportVersion:
    claim = await get_visible_claim(db, principal, claim_id, lock=True)
    busy = await db.scalar(
        select(ReportVersion.id).where(
            ReportVersion.claim_id == claim.id, ReportVersion.format == fmt, ReportVersion.status == "queued"
        )
    )
    if busy is not None:
        raise ConflictError("A report in this format is already being generated for this claim")
    last = await db.scalar(
        select(func.max(ReportVersion.version_no)).where(
            ReportVersion.claim_id == claim.id, ReportVersion.format == fmt
        )
    )
    decision = await db.scalar(
        select(ClaimDecision.id)
        .where(ClaimDecision.claim_id == claim.id)
        .order_by(ClaimDecision.created_at.desc())
        .limit(1)
    )
    report = ReportVersion(
        id=uuid.uuid4(),
        organization_id=claim.organization_id,
        claim_id=claim.id,
        decision_id=decision,
        version_no=(last or 0) + 1,
        format=fmt,
        status="queued",
        template_version=report_builder.TEMPLATE_VERSION,
        requested_by=principal.user_id,
        created_at=_now(),
    )
    db.add(report)
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="report.requested",
        entity_type="claim",
        entity_id=claim.id,
        after={"report_id": str(report.id), "format": fmt, "version": report.version_no},
    )
    return report


def enqueue(report: ReportVersion) -> None:
    from tasks.celery_app import celery_app

    celery_app.send_task(
        "reports.generate", args=[str(report.organization_id), str(report.id)], queue="reports"
    )


async def generate(db: Database, settings: Settings, organization_id: uuid.UUID, report_id: uuid.UUID) -> str:
    """Worker side. Returns the final status; a failure is recorded on the version, not raised."""
    async with db.sessionmaker() as s:
        await bind_tenant(s, organization_id)
        report = await s.scalar(select(ReportVersion).where(ReportVersion.id == report_id).with_for_update())
        if report is None or report.status != "queued":
            return report.status if report else "missing"
        claim = await s.get(Claim, report.claim_id)
        assert claim is not None
        try:
            content = await report_builder.build(s, claim.id, version_no=report.version_no)
            data = (
                report_pdf.render(content)
                if report.format == "pdf"
                else json.dumps(content, indent=2, ensure_ascii=False, default=str).encode()
            )
            key = f"{organization_id}/{claim.id}/report-v{report.version_no}.{report.format}"
            await build_storage(settings).put_bytes(
                settings.s3_bucket_reports, key, data, CONTENT_TYPES[report.format]
            )
        except Exception as exc:  # recorded on the version so the requester sees why
            report.status, report.error, report.generated_at = (
                "failed",
                f"{type(exc).__name__}: {exc}"[:500],
                _now(),
            )
            await s.commit()
            return report.status
        report.status, report.storage_key = "ready", key
        report.sha256, report.size_bytes = hashlib.sha256(data).hexdigest(), len(data)
        report.evidence_count, report.generated_at = len(content["evidence"]), _now()
        notices = []
        fmt = report.format.upper()
        if report.requested_by:
            notices.append(
                events.Notice(
                    [report.requested_by],
                    "report.ready",
                    f"Report v{report.version_no} ({fmt}) ready for {claim.claim_reference}",
                    f"The {fmt} report for claim {claim.claim_reference} has been generated.",
                    link=f"/claims/{claim.id}?tab=reports",
                )
            )
        await events.emit(
            s,
            organization_id,
            "report.ready",
            events.claim_data(
                claim,
                report_id=str(report.id),
                format=report.format,
                version_no=report.version_no,
                sha256=report.sha256,
            ),
            claim_id=claim.id,
            notices=notices,
        )
        await s.commit()
        return report.status


async def list_for_claim(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> list[ReportVersion]:
    claim = await get_visible_claim(db, principal, claim_id)
    stmt = select(ReportVersion).where(ReportVersion.claim_id == claim.id)
    return list((await db.scalars(stmt.order_by(ReportVersion.created_at.desc()))).all())


async def recent(db: AsyncSession, principal: Principal, limit: int = 50) -> list[tuple[ReportVersion, str]]:
    from app.services.claim_access import scope_claims

    stmt = scope_claims(
        select(ReportVersion, Claim.claim_reference).join(Claim, Claim.id == ReportVersion.claim_id),
        principal,
    )
    rows = await db.execute(stmt.order_by(ReportVersion.created_at.desc()).limit(min(limit, 200)))
    return [(r, ref) for r, ref in rows.all()]


async def download(
    db: AsyncSession, principal: Principal, actor: AuditActor, settings: Settings, report_id: uuid.UUID
) -> tuple[ReportVersion, Claim, bytes]:
    report = await db.get(ReportVersion, report_id)
    if report is None or report.organization_id != principal.organization_id:
        raise NotFoundError("Report not found")
    claim = await get_visible_claim(db, principal, report.claim_id)
    if report.status != "ready" or report.storage_key is None:
        raise ConflictError(f"This report is {report.status}")
    data = await build_storage(settings).get_bytes(settings.s3_bucket_reports, report.storage_key)
    if hashlib.sha256(data).hexdigest() != report.sha256:
        raise ConflictError("The stored report does not match its recorded hash; it was not served")
    await audit_service.record(
        db,
        actor=actor,
        action="report.downloaded",
        entity_type="claim",
        entity_id=claim.id,
        after={"report_id": str(report.id), "sha256": report.sha256},
    )
    return report, claim, data
