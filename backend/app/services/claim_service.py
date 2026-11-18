"""Claim intake, listing, editing and status changes.

Intake is the deterministic Intake Agent core (docs/architecture/03-agent-contracts.md):
it validates ownership and amounts, resolves the policy version in force on the incident
date, detects possible duplicates, sets priority and lists the required documents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, NotFoundError
from app.models.catalog import Customer, DocumentRequirement, InsuranceProduct, Policy
from app.models.claims import Claim, ClaimAssignment, ClaimRelation, ClaimStatusChange
from app.models.identity import User
from app.security.principal import Principal
from app.services import audit_service, policy_service, similarity
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim, get_writable_claim, scope_claims
from app.services.common import check_version, ilike_any, jsonable
from rules_engine.claim_status import ClaimStatus, InvalidTransitionError, allowed_targets, assert_transition
from rules_engine.intake import LOSS_TYPES, PolicyTerms, assess, duplicate_window
from rules_engine.money import MoneyError, validate_amount

EDITABLE_STATUSES = frozenset({ClaimStatus.SUBMITTED, ClaimStatus.AWAITING_INFORMATION})
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


@dataclass(frozen=True, slots=True)
class NewClaim:
    customer_id: uuid.UUID
    policy_id: uuid.UUID
    loss_type: str
    incident_date: date
    incident_time: time | None
    incident_location: dict[str, Any] | None
    description: str
    claimed_amount: Decimal
    currency: str
    channel: str = "staff"
    is_synthetic: bool = False


@dataclass(frozen=True, slots=True)
class ClaimFilters:
    statuses: list[str]
    priority: str | None = None
    product_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    unassigned: bool = False
    q: str | None = None
    incident_from: date | None = None
    incident_to: date | None = None
    is_synthetic: bool | None = None
    sort: str = "-created_at"


@dataclass(frozen=True, slots=True)
class IntakeResult:
    required_documents: list[str]
    missing_documents: list[str]
    duplicates: list[dict[str, str]]


async def _next_reference(db: AsyncSession, org_id: uuid.UUID, year: int) -> str:
    value = await db.scalar(
        text(
            "INSERT INTO claim_reference_counters (organization_id, year, last_value) VALUES (:o, :y, 1) "
            "ON CONFLICT (organization_id, year) DO UPDATE "
            "SET last_value = claim_reference_counters.last_value + 1 RETURNING last_value"
        ),
        {"o": org_id, "y": year},
    )
    return f"CLM-{year}-{int(value or 0):06d}"


async def required_documents(db: AsyncSession, product_id: uuid.UUID) -> list[str]:
    rows = await db.scalars(
        select(DocumentRequirement.document_type)
        .where(DocumentRequirement.product_id == product_id, DocumentRequirement.mandatory.is_(True))
        .order_by(DocumentRequirement.document_type)
    )
    return list(rows.all())


async def _duplicates(db: AsyncSession, claim: Claim) -> list[Claim]:
    start, end = duplicate_window(claim.incident_date)
    rows = await db.scalars(
        select(Claim).where(
            Claim.organization_id == claim.organization_id,
            Claim.policy_id == claim.policy_id,
            Claim.id != claim.id,
            Claim.deleted_at.is_(None),
            Claim.incident_date.between(start, end),
        )
    )
    return list(rows.all())


def _validate_amount(amount: Decimal, currency: str) -> Decimal:
    try:
        return validate_amount(amount, currency)
    except MoneyError as exc:
        raise BusinessRuleError(str(exc)) from exc


async def _assess(
    db: AsyncSession, claim: Claim, policy: Policy
) -> tuple[list[Claim], str, list[dict[str, str]]]:
    version = await policy_service.version_on(db, policy.id, claim.incident_date)
    claim.policy_version_id = version.id if version else None
    duplicates = await _duplicates(db, claim)
    terms = (
        PolicyTerms(
            version.effective_from,
            version.effective_to,
            version.sum_insured,
            version.currency,
            version.waiting_period_days,
        )
        if version
        else None
    )
    result = assess(
        incident_date=claim.incident_date,
        reported_on=claim.reported_at.date(),
        claimed_amount=claim.claimed_amount,
        currency=claim.currency,
        terms=terms,
        policy_status=policy.status,
        duplicate_references=[d.claim_reference for d in duplicates],
    )
    return duplicates, result.priority, [f.as_dict() for f in result.findings]


async def create_claim(
    db: AsyncSession, principal: Principal, actor: AuditActor, data: NewClaim
) -> tuple[Claim, IntakeResult]:
    org = principal.organization_id
    policy = await db.scalar(
        select(Policy).where(
            Policy.organization_id == org, Policy.id == data.policy_id, Policy.deleted_at.is_(None)
        )
    )
    if policy is None:
        raise NotFoundError("Policy not found")
    if policy.customer_id != data.customer_id:
        raise BusinessRuleError("The policy does not belong to the selected customer")
    product = await db.get(InsuranceProduct, policy.product_id)
    assert product is not None
    if data.loss_type not in LOSS_TYPES[product.line]:
        raise BusinessRuleError(f"'{data.loss_type}' is not a valid loss type for {product.line} products")
    now = datetime.now(UTC)
    if data.incident_date > now.date():
        raise BusinessRuleError("The incident date cannot be in the future")
    amount = _validate_amount(data.claimed_amount, data.currency)

    claim = Claim(
        organization_id=org,
        claim_reference=await _next_reference(db, org, now.year),
        customer_id=policy.customer_id,
        policy_id=policy.id,
        product_id=product.id,
        status=ClaimStatus.SUBMITTED.value,
        category=data.loss_type,
        incident_date=data.incident_date,
        incident_time=data.incident_time,
        reported_at=now,
        incident_location=data.incident_location,
        description=data.description,
        claimed_amount=amount,
        currency=data.currency,
        channel=data.channel,
        is_synthetic=data.is_synthetic,
        created_by=principal.user_id,
    )
    db.add(claim)
    await db.flush()
    duplicates, claim.priority, claim.intake_findings = await _assess(db, claim, policy)
    for dup in duplicates:
        db.add(
            ClaimRelation(
                organization_id=org,
                claim_id=claim.id,
                related_claim_id=dup.id,
                relation="duplicate_suspected",
                source="rule",
                detail=f"Same policy, incident within ±3 days ({dup.claim_reference})",
            )
        )
    db.add(
        ClaimStatusChange(
            organization_id=org,
            claim_id=claim.id,
            from_status=None,
            to_status=claim.status,
            reason="Claim submitted",
            actor_type="user",
            changed_by=principal.user_id,
        )
    )
    await audit_service.record(
        db,
        actor=actor,
        action="claim.created",
        entity_type="claim",
        entity_id=claim.id,
        after={
            "claim_reference": claim.claim_reference,
            "policy_id": str(policy.id),
            "claimed_amount": str(amount),
            "currency": claim.currency,
            "priority": claim.priority,
            "findings": [f["code"] for f in claim.intake_findings],
        },
    )
    await db.flush()
    await similarity.upsert(db, claim)  # Phase 12: similar-claim search
    required = await required_documents(db, product.id)
    intake = IntakeResult(
        required,
        list(required),  # no documents can exist yet (uploads arrive in Phase 4)
        [{"id": str(d.id), "claim_reference": d.claim_reference} for d in duplicates],
    )
    return claim, intake


async def list_claims(
    db: AsyncSession, principal: Principal, filters: ClaimFilters, *, page: int, page_size: int
) -> tuple[list[tuple[Claim, Customer, Policy, InsuranceProduct, User | None]], int]:
    adjuster = (
        select(ClaimAssignment.claim_id, ClaimAssignment.user_id)
        .where(ClaimAssignment.role_on_claim == "adjuster", ClaimAssignment.unassigned_at.is_(None))
        .subquery()
    )
    stmt = (
        select(Claim, Customer, Policy, InsuranceProduct, User)
        .join(Customer, Customer.id == Claim.customer_id)
        .join(Policy, Policy.id == Claim.policy_id)
        .join(InsuranceProduct, InsuranceProduct.id == Claim.product_id)
        .outerjoin(adjuster, adjuster.c.claim_id == Claim.id)
        .outerjoin(User, User.id == adjuster.c.user_id)
    )
    stmt = scope_claims(stmt, principal)
    if filters.statuses:
        stmt = stmt.where(Claim.status.in_(filters.statuses))
    if filters.priority:
        stmt = stmt.where(Claim.priority == filters.priority)
    if filters.product_id:
        stmt = stmt.where(Claim.product_id == filters.product_id)
    if filters.assignee_id:
        stmt = stmt.where(adjuster.c.user_id == filters.assignee_id)
    if filters.unassigned:
        stmt = stmt.where(adjuster.c.user_id.is_(None))
    if filters.q:
        stmt = stmt.where(
            ilike_any(filters.q, Claim.claim_reference, Customer.full_name, Policy.policy_number)
        )
    if filters.incident_from:
        stmt = stmt.where(Claim.incident_date >= filters.incident_from)
    if filters.incident_to:
        stmt = stmt.where(Claim.incident_date <= filters.incident_to)
    if filters.is_synthetic is not None:
        stmt = stmt.where(Claim.is_synthetic.is_(filters.is_synthetic))

    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    desc = filters.sort.startswith("-")
    key = filters.sort.lstrip("-")
    if key == "priority":
        column: Any = func.array_position(
            text("ARRAY['urgent','high','normal','low']::varchar[]"), Claim.priority
        )
        desc = not desc  # "-priority" lists the most urgent first
    else:
        column = {
            "created_at": Claim.created_at,
            "incident_date": Claim.incident_date,
            "claimed_amount": Claim.claimed_amount,
            "claim_reference": Claim.claim_reference,
        }[key]
    order = column.desc() if desc else column.asc()
    rows = (
        await db.execute(stmt.order_by(order, Claim.id).offset((page - 1) * page_size).limit(page_size))
    ).all()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows], total


async def get_claim(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> Claim:
    return await get_visible_claim(db, principal, claim_id)


def manual_targets(claim: Claim) -> list[str]:
    return [s.value for s in allowed_targets(ClaimStatus(claim.status), "human")]


async def update_claim(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    *,
    expected_version: int,
    changes: dict[str, Any],
) -> Claim:
    claim = await get_writable_claim(db, principal, claim_id)
    check_version(claim.row_version, expected_version, "claim")
    material = {"incident_date", "claimed_amount", "category"}
    if material & changes.keys() and ClaimStatus(claim.status) not in EDITABLE_STATUSES:
        raise BusinessRuleError(
            "Incident date, amount and loss type can only change before processing starts"
        )
    if "category" in changes:
        product = await db.get(InsuranceProduct, claim.product_id)
        assert product is not None
        if changes["category"] not in LOSS_TYPES[product.line]:
            raise BusinessRuleError(
                f"'{changes['category']}' is not a valid loss type for {product.line} products"
            )
    if "incident_date" in changes and changes["incident_date"] > datetime.now(UTC).date():
        raise BusinessRuleError("The incident date cannot be in the future")
    if "claimed_amount" in changes:
        changes["claimed_amount"] = _validate_amount(changes["claimed_amount"], claim.currency)

    before = {k: getattr(claim, k) for k in changes if getattr(claim, k) != changes[k]}
    after = {k: changes[k] for k in before}
    for key, value in after.items():
        setattr(claim, key, value)
    if not after:
        return claim
    if material & after.keys():
        policy = await db.get(Policy, claim.policy_id)
        assert policy is not None
        _, priority, claim.intake_findings = await _assess(db, claim, policy)
        if "priority" not in changes:  # a manual priority choice wins over the recomputed one
            claim.priority = priority
    claim.row_version += 1
    await similarity.upsert(db, claim)  # no-op unless the text changed
    await audit_service.record(
        db,
        actor=actor,
        action="claim.updated",
        entity_type="claim",
        entity_id=claim.id,
        before=jsonable(before),
        after=jsonable(after),
    )
    await db.commit()
    return await get_claim(db, principal, claim.id)


async def change_status(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    *,
    target: str,
    reason: str,
    expected_version: int,
) -> Claim:
    claim = await get_writable_claim(db, principal, claim_id)
    check_version(claim.row_version, expected_version, "claim")
    current = ClaimStatus(claim.status)
    try:
        assert_transition(current, ClaimStatus(target), "human")
    except (InvalidTransitionError, ValueError) as exc:
        raise BusinessRuleError(str(exc)) from exc
    claim.status = target
    claim.row_version += 1
    db.add(
        ClaimStatusChange(
            organization_id=claim.organization_id,
            claim_id=claim.id,
            from_status=current.value,
            to_status=target,
            reason=reason,
            actor_type="user",
            changed_by=principal.user_id,
        )
    )
    await audit_service.record(
        db,
        actor=actor,
        action="claim.status_changed",
        entity_type="claim",
        entity_id=claim.id,
        before={"status": current.value},
        after={"status": target, "reason": reason},
    )
    await db.commit()
    return await get_claim(db, principal, claim.id)


def sort_key_valid(sort: str) -> bool:
    return sort.lstrip("-") in {
        "created_at",
        "incident_date",
        "claimed_amount",
        "claim_reference",
        "priority",
    }
