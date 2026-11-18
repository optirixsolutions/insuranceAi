from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import DbDep, require
from app.core.errors import BusinessRuleError
from app.models.audit import AuditEvent
from app.models.identity import User
from app.schemas.common import CursorPage
from app.schemas.identity import AuditEventOut, AuditVerificationOut
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit"])
Auditor = Annotated[Principal, Depends(require(P.AUDIT_READ))]


def _encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"seq:{seq}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        prefix, value = raw.split(":", 1)
        if prefix != "seq":
            raise ValueError(prefix)
        return int(value)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise BusinessRuleError("Invalid cursor") from exc


@router.get("", response_model=CursorPage[AuditEventOut])
async def list_audit_events(
    principal: Auditor,
    db: DbDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    action: Annotated[str | None, Query(max_length=80)] = None,
    entity_type: Annotated[str | None, Query(max_length=60)] = None,
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
) -> CursorPage[AuditEventOut]:
    """Newest first. Pass ``next_cursor`` back as ``cursor`` for the next page."""
    stmt = select(AuditEvent).where(AuditEvent.organization_id == principal.organization_id)
    if cursor:
        stmt = stmt.where(AuditEvent.seq < _decode_cursor(cursor))
    if action:
        stmt = stmt.where(AuditEvent.action.startswith(action, autoescape=True))
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if actor_id:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if occurred_from:
        stmt = stmt.where(AuditEvent.occurred_at >= occurred_from)
    if occurred_to:
        stmt = stmt.where(AuditEvent.occurred_at < occurred_to)
    rows = list((await db.scalars(stmt.order_by(AuditEvent.seq.desc()).limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    actor_ids = {r.actor_id for r in rows if r.actor_id}
    names: dict[uuid.UUID, str] = {}
    if actor_ids:
        result = await db.execute(
            select(User.id, User.display_name).where(
                User.organization_id == principal.organization_id, User.id.in_(actor_ids)
            )
        )
        names = {uid: name for uid, name in result.all()}
    items = []
    for r in rows:
        item = AuditEventOut.model_validate(r)
        item.actor_name = names.get(r.actor_id) if r.actor_id else None
        items.append(item)
    return CursorPage(items=items, next_cursor=_encode_cursor(rows[-1].seq) if has_more and rows else None)


@router.get("/verify", response_model=AuditVerificationOut)
async def verify_audit_chain(_: Auditor, db: DbDep) -> AuditVerificationOut:
    """Recompute the organisation's hash chain and report the first inconsistency, if any."""
    result = await audit_service.verify_chain(db)
    return AuditVerificationOut(
        valid=result.valid,
        events_checked=result.events_checked,
        first_invalid_seq=result.first_invalid_seq,
        reason=result.reason,
    )
