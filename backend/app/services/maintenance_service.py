"""Nightly platform maintenance (Phase 13).

* ``run_retention`` deletes expired platform data through the ``claimos_retention`` definer function
  (sessions, login attempts, sent notifications, finished webhook deliveries, old checkpoints, expired
  idempotency keys and MFA challenges) and records a ``RetentionRun`` with the counts. Business records
  (claims, decisions, documents, reports) and audit events are never touched.
* ``verify_audit_chains`` re-computes every organisation's audit hash chain, stores an
  ``AuditVerification`` and, if the chain is broken, notifies the people who can read the audit log.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.db import Database
from app.core.tenancy import bind_tenant
from app.models.audit import AuditEvent
from app.models.security import AuditVerification, RetentionRun
from app.security.permissions import P
from app.services import audit_service, events

log = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


async def run_retention(db: Database, settings: Settings) -> dict[str, int]:
    run = RetentionRun(id=uuid.uuid4(), started_at=_now())
    async with db.sessionmaker() as s:
        try:
            deleted: dict[str, int] = (
                await s.execute(
                    text(
                        "SELECT claimos_retention("
                        ":sessions, :attempts, :notifications, :deliveries, :checkpoints)"
                    ),
                    {
                        "sessions": settings.retention_session_days,
                        "attempts": settings.retention_login_attempt_days,
                        "notifications": settings.retention_notification_days,
                        "deliveries": settings.retention_webhook_delivery_days,
                        "checkpoints": settings.retention_checkpoint_days,
                    },
                )
            ).scalar_one()
        except Exception as exc:
            await s.rollback()
            run.error, run.finished_at = f"{type(exc).__name__}: {exc}"[:500], _now()
            s.add(run)
            await s.commit()
            log.error("retention_failed", error=run.error)
            raise
        run.deleted, run.finished_at = deleted, _now()
        s.add(run)
        await s.commit()
    log.info("retention_completed", deleted=deleted)
    return deleted


async def _verify_one(db: Database, organization_id: uuid.UUID) -> AuditVerification:
    async with db.sessionmaker() as s:
        await bind_tenant(s, organization_id)
        result = await audit_service.verify_chain(s)
        bad_id = None
        if result.first_invalid_seq is not None:
            bad_id = await s.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.organization_id == organization_id, AuditEvent.seq == result.first_invalid_seq
                )
            )
        check = AuditVerification(
            id=uuid.uuid4(),
            organization_id=organization_id,
            ok=result.valid,
            events_checked=result.events_checked,
            first_bad_event_id=bad_id,
            detail=(f"seq {result.first_invalid_seq}: {result.reason}" if not result.valid else None),
            checked_at=_now(),
        )
        s.add(check)
        if not result.valid:
            people = await events.holders_of(s, organization_id, P.AUDIT_READ.value)
            await events.emit(
                s,
                organization_id,
                "audit.chain_broken",
                {"first_invalid_seq": result.first_invalid_seq, "reason": result.reason},
                notices=[
                    events.Notice(
                        people,
                        "audit.chain_broken",
                        "Audit log integrity check failed",
                        f"The audit hash chain does not verify from event #{result.first_invalid_seq} "
                        f"({result.reason}). Investigate before relying on the audit log.",
                        link="/audit",
                        email=True,
                    )
                ],
            )
            log.error(
                "audit_chain_broken", organization_id=str(organization_id), seq=result.first_invalid_seq
            )
        await s.commit()
        return check


async def verify_audit_chains(db: Database, settings: Settings) -> dict[str, Any]:
    del settings  # same signature as the other scheduled jobs
    async with db.sessionmaker() as s:
        org_ids = list((await s.scalars(text("SELECT claimos_active_organization_ids()"))).all())
    checked, broken = 0, []
    for org_id in org_ids:
        check = await _verify_one(db, org_id)
        checked += 1
        if not check.ok:
            broken.append(str(org_id))
    log.info("audit_chains_verified", organizations=checked, broken=len(broken))
    return {"organizations": checked, "broken": broken}
