"""Claim assignment, internal notes, activity timeline, related claims and CSV export."""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ForbiddenError, NotFoundError
from app.models.claims import Claim, ClaimAssignment, ClaimNote, ClaimRelation, ClaimStatusChange
from app.models.identity import User
from app.repositories import identity as identity_repo
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim, sees_all_claims
from app.services.claim_service import ClaimFilters, list_claims

AssignmentRole = Literal["adjuster", "reviewer", "investigator"]
# The permission an assignee must hold for each role on a claim.
ROLE_PERMISSION: dict[str, str] = {
    "adjuster": P.CLAIMS_WRITE.value,
    "reviewer": P.REVIEWS_DECIDE.value,
    "investigator": P.FRAUD_INVESTIGATE.value,
}
EXPORT_LIMIT = 5000


@dataclass(frozen=True, slots=True)
class AssignmentView:
    role_on_claim: str
    user_id: uuid.UUID
    display_name: str
    assigned_at: datetime


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    at: datetime
    kind: str
    title: str
    detail: str | None
    actor_name: str | None


def _now() -> datetime:
    return datetime.now(UTC)


async def assignments(db: AsyncSession, claim_id: uuid.UUID) -> list[AssignmentView]:
    rows = (
        await db.execute(
            select(ClaimAssignment, User.display_name)
            .join(User, User.id == ClaimAssignment.user_id)
            .where(ClaimAssignment.claim_id == claim_id, ClaimAssignment.unassigned_at.is_(None))
            .order_by(ClaimAssignment.role_on_claim)
        )
    ).all()
    return [AssignmentView(a.role_on_claim, a.user_id, name, a.assigned_at) for a, name in rows]


async def assignable_users(db: AsyncSession, principal: Principal, role: AssignmentRole) -> list[User]:
    users = (
        await db.scalars(
            select(User)
            .where(
                User.organization_id == principal.organization_id,
                User.status == "active",
                User.deleted_at.is_(None),
            )
            .order_by(User.display_name)
        )
    ).all()
    needed = ROLE_PERMISSION[role]
    return [
        u
        for u in users
        if needed in await identity_repo.user_permissions(db, principal.organization_id, u.id)
    ]


async def assign(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    *,
    role: AssignmentRole,
    user_id: uuid.UUID | None,
) -> list[AssignmentView]:
    claim = await get_visible_claim(db, principal, claim_id, lock=True)
    current = await db.scalar(
        select(ClaimAssignment).where(
            ClaimAssignment.claim_id == claim.id,
            ClaimAssignment.role_on_claim == role,
            ClaimAssignment.unassigned_at.is_(None),
        )
    )
    if user_id is not None:
        user = await identity_repo.get_user(db, principal.organization_id, user_id)
        if user is None or user.status != "active":
            raise BusinessRuleError("Assignee must be an active user of this organisation")
        perms = await identity_repo.user_permissions(db, principal.organization_id, user.id)
        if ROLE_PERMISSION[role] not in perms:
            raise BusinessRuleError(f"This user cannot act as {role}: missing '{ROLE_PERMISSION[role]}'")
    if current is not None and current.user_id == user_id:
        return await assignments(db, claim.id)
    if current is not None:
        current.unassigned_at = _now()
        await db.flush()
    if user_id is not None:
        db.add(
            ClaimAssignment(
                organization_id=claim.organization_id,
                claim_id=claim.id,
                user_id=user_id,
                role_on_claim=role,
                assigned_by=principal.user_id,
                assigned_at=_now(),
            )
        )
    await audit_service.record(
        db,
        actor=actor,
        action="claim.assigned" if user_id else "claim.unassigned",
        entity_type="claim",
        entity_id=claim.id,
        before={"role": role, "user_id": str(current.user_id) if current else None},
        after={"role": role, "user_id": str(user_id) if user_id else None},
    )
    await db.commit()
    return await assignments(db, claim.id)


def _can_see_restricted(principal: Principal) -> bool:
    return sees_all_claims(principal)


async def list_notes(
    db: AsyncSession, principal: Principal, claim_id: uuid.UUID
) -> list[tuple[ClaimNote, str]]:
    claim = await get_visible_claim(db, principal, claim_id)
    stmt = (
        select(ClaimNote, User.display_name)
        .join(User, User.id == ClaimNote.author_id)
        .where(ClaimNote.claim_id == claim.id, ClaimNote.deleted_at.is_(None))
    )
    if not _can_see_restricted(principal):
        stmt = stmt.where(or_(ClaimNote.visibility == "internal", ClaimNote.author_id == principal.user_id))
    rows = (await db.execute(stmt.order_by(ClaimNote.created_at.desc()))).all()
    return [(n, name) for n, name in rows]


async def add_note(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    *,
    body: str,
    visibility: Literal["internal", "restricted"],
) -> ClaimNote:
    claim = await get_visible_claim(db, principal, claim_id)
    if visibility == "restricted" and not _can_see_restricted(principal):
        raise ForbiddenError("Only managers and oversight roles can write restricted notes")
    note = ClaimNote(
        organization_id=claim.organization_id,
        claim_id=claim.id,
        author_id=principal.user_id,
        visibility=visibility,
        body=body,
    )
    db.add(note)
    await db.flush()
    # The note text itself stays in the notes table; the audit log records that it exists.
    await audit_service.record(
        db,
        actor=actor,
        action="claim.note_added",
        entity_type="claim_note",
        entity_id=note.id,
        after={"claim_id": str(claim.id), "visibility": visibility, "length": len(body)},
    )
    await db.commit()
    return note


async def delete_note(
    db: AsyncSession, principal: Principal, actor: AuditActor, claim_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    claim = await get_visible_claim(db, principal, claim_id)
    note = await db.scalar(
        select(ClaimNote).where(
            ClaimNote.id == note_id, ClaimNote.claim_id == claim.id, ClaimNote.deleted_at.is_(None)
        )
    )
    if note is None:
        raise NotFoundError("Note not found")
    if note.author_id != principal.user_id and not principal.has(P.CLAIMS_ASSIGN.value):
        raise ForbiddenError("Only the author or a claims manager can delete this note")
    note.deleted_at = _now()
    await audit_service.record(
        db,
        actor=actor,
        action="claim.note_deleted",
        entity_type="claim_note",
        entity_id=note.id,
        after={"claim_id": str(claim.id)},
    )
    await db.commit()


async def timeline(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> list[TimelineEntry]:
    claim = await get_visible_claim(db, principal, claim_id)
    names: dict[uuid.UUID, str] = {}

    async def name(user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in names:
            user = await db.get(User, user_id)
            names[user_id] = user.display_name if user else "Unknown user"
        return names[user_id]

    entries: list[TimelineEntry] = []
    for change in (
        await db.scalars(select(ClaimStatusChange).where(ClaimStatusChange.claim_id == claim.id))
    ).all():
        title = (
            "Claim submitted"
            if change.from_status is None
            else f"Status: {change.from_status} → {change.to_status}"
        )
        entries.append(
            TimelineEntry(change.changed_at, "status", title, change.reason, await name(change.changed_by))
        )
    for a in (await db.scalars(select(ClaimAssignment).where(ClaimAssignment.claim_id == claim.id))).all():
        who = await name(a.user_id)
        entries.append(
            TimelineEntry(
                a.assigned_at,
                "assignment",
                f"{who} assigned as {a.role_on_claim}",
                None,
                await name(a.assigned_by),
            )
        )
        if a.unassigned_at:
            entries.append(
                TimelineEntry(
                    a.unassigned_at, "assignment", f"{who} unassigned as {a.role_on_claim}", None, None
                )
            )
    for note, author in await list_notes(db, principal, claim.id):
        entries.append(
            TimelineEntry(
                note.created_at,
                "note",
                "Restricted note added" if note.visibility == "restricted" else "Note added",
                note.body[:280],
                author,
            )
        )
    return sorted(entries, key=lambda e: e.at, reverse=True)


async def related(
    db: AsyncSession, principal: Principal, claim_id: uuid.UUID
) -> list[tuple[ClaimRelation, Claim]]:
    claim = await get_visible_claim(db, principal, claim_id)
    rows = (
        await db.scalars(
            select(ClaimRelation).where(
                or_(ClaimRelation.claim_id == claim.id, ClaimRelation.related_claim_id == claim.id)
            )
        )
    ).all()
    out: list[tuple[ClaimRelation, Claim]] = []
    for rel in rows:
        other_id = rel.related_claim_id if rel.claim_id == claim.id else rel.claim_id
        other = await db.get(Claim, other_id)
        if other is not None and other.deleted_at is None:
            out.append((rel, other))
    return out


def _safe_cell(value: Any) -> str:
    """Neutralise spreadsheet formula injection (cells starting with = + - @ tab or CR)."""
    text_value = "" if value is None else str(value)
    return "'" + text_value if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value


async def export_csv(db: AsyncSession, principal: Principal, filters: ClaimFilters) -> str:
    rows, total = await list_claims(db, principal, filters, page=1, page_size=EXPORT_LIMIT)
    if total > EXPORT_LIMIT:
        raise BusinessRuleError(
            f"Narrow the filters: exports are limited to {EXPORT_LIMIT} claims ({total} match)"
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "claim_reference",
            "status",
            "priority",
            "loss_type",
            "customer",
            "policy_number",
            "product",
            "incident_date",
            "claimed_amount",
            "currency",
            "adjuster",
            "is_synthetic",
            "created_at",
        ]
    )
    for claim, customer, policy, product, adjuster in rows:
        writer.writerow(
            [
                _safe_cell(v)
                for v in (
                    claim.claim_reference,
                    claim.status,
                    claim.priority,
                    claim.category,
                    customer.full_name,
                    policy.policy_number,
                    product.name,
                    claim.incident_date.isoformat(),
                    claim.claimed_amount,
                    claim.currency,
                    adjuster.display_name if adjuster else "",
                    claim.is_synthetic,
                    claim.created_at.isoformat(),
                )
            ]
        )
    return buffer.getvalue()
