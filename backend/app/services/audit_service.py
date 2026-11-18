"""Append-only, hash-chained audit log.

Each organisation has its own chain: ``hash = sha256(prev_hash + canonical_json(event))``.
Writes take a transaction-scoped advisory lock per organisation, so ``seq`` is gap-free
and the chain is linear. Audit rows are written in the same transaction as the change
they describe; if the change rolls back, so does its audit event.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import bound_tenant
from app.models.audit import AuditEvent

GENESIS_HASH = "0" * 64
ActorType = Literal["user", "system", "agent", "integration"]


@dataclass(frozen=True, slots=True)
class AuditActor:
    actor_type: ActorType
    actor_id: uuid.UUID | None
    request_id: str | None = None
    ip: str | None = None


def _canonical(event: dict[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + _canonical(payload)).hexdigest()


def _payload(e: AuditEvent) -> dict[str, Any]:
    return {
        "organization_id": str(e.organization_id),
        "seq": e.seq,
        "occurred_at": e.occurred_at.astimezone(UTC).isoformat(),
        "actor_type": e.actor_type,
        "actor_id": str(e.actor_id) if e.actor_id else None,
        "action": e.action,
        "entity_type": e.entity_type,
        "entity_id": str(e.entity_id) if e.entity_id else None,
        "request_id": e.request_id,
        "ip": str(e.ip) if e.ip else None,
        "before": e.before,
        "after": e.after,
    }


async def record(
    db: AsyncSession,
    *,
    actor: AuditActor,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    org_id = bound_tenant(db)
    if org_id is None:
        raise RuntimeError("audit events require a tenant-bound session")
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"audit:{org_id}"})
    last = (
        await db.execute(
            select(AuditEvent.seq, AuditEvent.hash)
            .where(AuditEvent.organization_id == org_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).first()
    seq, prev_hash = (last[0] + 1, last[1]) if last else (1, GENESIS_HASH)
    event = AuditEvent(
        id=uuid.uuid4(),
        organization_id=org_id,
        seq=seq,
        occurred_at=datetime.now(UTC),
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=actor.request_id,
        ip=actor.ip,
        before=before,
        after=after,
        prev_hash=prev_hash,
    )
    event.hash = compute_hash(prev_hash, _payload(event))
    db.add(event)
    await db.flush()
    return event


@dataclass(frozen=True, slots=True)
class ChainVerification:
    valid: bool
    events_checked: int
    first_invalid_seq: int | None
    reason: str | None


async def verify_chain(db: AsyncSession, *, batch_size: int = 1000) -> ChainVerification:
    org_id = bound_tenant(db)
    if org_id is None:
        raise RuntimeError("chain verification requires a tenant-bound session")
    prev_hash, expected_seq, checked, last_seq = GENESIS_HASH, 1, 0, 0
    while True:
        rows = (
            await db.scalars(
                select(AuditEvent)
                .where(AuditEvent.organization_id == org_id, AuditEvent.seq > last_seq)
                .order_by(AuditEvent.seq)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            return ChainVerification(True, checked, None, None)
        for e in rows:
            if e.seq != expected_seq:
                return ChainVerification(False, checked, e.seq, f"sequence gap: expected {expected_seq}")
            if e.prev_hash != prev_hash:
                return ChainVerification(False, checked, e.seq, "previous hash does not match")
            if compute_hash(prev_hash, _payload(e)) != e.hash:
                return ChainVerification(False, checked, e.seq, "event content does not match its hash")
            prev_hash, expected_seq, last_seq, checked = e.hash, e.seq + 1, e.seq, checked + 1
