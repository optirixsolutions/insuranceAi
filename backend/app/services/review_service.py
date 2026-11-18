"""Human review (Phase 9): queue, ownership, decisions, four-eyes, escalation, authorities.

A review row is opened by the HUMAN_REVIEW node (one per interrupt). Every decision is checked
against ``rules_engine.review`` with persisted facts (never request data): the reviewer's
authorities, the claim's assigned adjuster, the recommendation, fraud level and rule-set
four-eyes threshold. Approvals and rejections write an append-only ``claim_decisions`` row and
move the claim in the same transaction; the caller commits and then enqueues the workflow resume.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AuthorityExceededError,
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.models.claims import Claim, ClaimAssignment
from app.models.rules import ClaimDecision, HumanReview, PendingDecision, ReviewerAuthority
from app.models.workflow import WorkflowExecution
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service, rule_set_service
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim
from rules_engine.review import Authority, DecisionContext, Ruling, rule
from rules_engine.rule_set import DEFAULT_CONFIG, RuleSetConfig

OPEN = ("pending", "in_progress", "awaiting_second_approval", "escalated")
PRIORITY = {"urgent": 40, "high": 25, "normal": 10, "low": 0}
Enqueue = Callable[[], None]


def _now() -> datetime:
    return datetime.now(UTC)


async def _config(db: AsyncSession, product_id: uuid.UUID) -> RuleSetConfig:
    active = await rule_set_service.active(db, product_id)
    return RuleSetConfig.model_validate(active.config) if active else DEFAULT_CONFIG


# --- opened by the workflow ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewFacts:
    recommendation: str | None
    payout: Decimal | None
    fraud_level: str | None
    fraud_score: int | None
    reasons: list[str]


async def open_for_interrupt(
    s: AsyncSession, execution: WorkflowExecution, interrupt_id: str, facts: ReviewFacts
) -> tuple[HumanReview, bool]:
    """Get or create the review for this interrupt (a node re-run after resume finds the same row)."""
    existing = await s.scalar(
        select(HumanReview).where(
            HumanReview.execution_id == execution.id, HumanReview.interrupt_id == interrupt_id
        )
    )
    if existing is not None:
        return existing, False
    claim = await s.get(Claim, execution.claim_id)
    assert claim is not None
    for stale in await s.scalars(
        select(HumanReview).where(HumanReview.claim_id == claim.id, HumanReview.status.in_(OPEN))
    ):  # an earlier run's review that was never closed
        stale.status, stale.outcome, stale.completed_at = "cancelled", "superseded", _now()
    await s.flush()
    config = await _config(s, claim.product_id)
    payout = facts.payout
    priority = (facts.fraud_score or 0) + PRIORITY.get(claim.priority, 10)
    priority += min(30, int((payout or claim.claimed_amount) / 1000))
    threshold = config.four_eyes_threshold
    four_eyes = facts.fraud_level in ("high", "critical") or (
        threshold is not None and payout is not None and payout > threshold
    )
    review = HumanReview(
        id=uuid.uuid4(),
        organization_id=claim.organization_id,
        claim_id=claim.id,
        execution_id=execution.id,
        interrupt_id=interrupt_id,
        recommendation=facts.recommendation,
        payout=payout,
        currency=claim.currency,
        fraud_level=facts.fraud_level,
        reasons=facts.reasons[:20],
        status="pending",
        priority_score=priority,
        due_at=_now() + timedelta(hours=config.review_sla_hours),
        four_eyes_required=four_eyes,
    )
    s.add(review)
    await s.flush()
    return review, True


async def cancel_open(s: AsyncSession, claim_id: uuid.UUID) -> None:
    for review in await s.scalars(
        select(HumanReview).where(HumanReview.claim_id == claim_id, HumanReview.status.in_(OPEN))
    ):
        review.status, review.outcome, review.completed_at = "cancelled", "processing_cancelled", _now()


# --- reads ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueFilters:
    status: str | None = None
    mine: bool = False
    risk_level: str | None = None
    product_id: uuid.UUID | None = None
    overdue: bool = False
    limit: int = 100


async def queue(db: AsyncSession, principal: Principal, f: QueueFilters) -> list[tuple[HumanReview, Claim]]:
    readers = (P.REVIEWS_DECIDE, P.CLAIMS_ASSIGN, P.FRAUD_INVESTIGATE)
    if not any(principal.has(p.value) for p in readers):
        raise ForbiddenError("The review queue needs the 'reviews.decide' permission")
    stmt = (
        select(HumanReview, Claim)
        .join(Claim, Claim.id == HumanReview.claim_id)
        .where(HumanReview.organization_id == principal.organization_id, Claim.deleted_at.is_(None))
    )
    if f.status:
        stmt = stmt.where(HumanReview.status == f.status)
    else:
        stmt = stmt.where(HumanReview.status.in_(OPEN))
    if f.mine:
        stmt = stmt.where(HumanReview.assigned_to == principal.user_id)
    if f.risk_level:
        stmt = stmt.where(HumanReview.fraud_level == f.risk_level)
    if f.product_id:
        stmt = stmt.where(Claim.product_id == f.product_id)
    if f.overdue:
        stmt = stmt.where(HumanReview.due_at < _now())
    stmt = stmt.order_by(HumanReview.priority_score.desc(), HumanReview.due_at)
    return [(r, c) for r, c in (await db.execute(stmt.limit(min(f.limit, 200)))).all()]


async def _claim_for_review(
    db: AsyncSession, principal: Principal, claim_id: uuid.UUID, *, lock: bool = False
) -> Claim:
    """Reviewers see a claim while it has an open review, even when not yet assigned to it."""
    try:
        return await get_visible_claim(db, principal, claim_id, lock=lock)
    except NotFoundError:
        if not principal.has(P.REVIEWS_DECIDE.value):
            raise
    stmt = select(Claim).where(
        Claim.id == claim_id,
        Claim.organization_id == principal.organization_id,
        Claim.deleted_at.is_(None),
        select(HumanReview.id).where(HumanReview.claim_id == Claim.id, HumanReview.status.in_(OPEN)).exists(),
    )
    claim = await db.scalar(stmt.with_for_update(key_share=True) if lock else stmt)
    if claim is None:
        raise NotFoundError("Claim not found")
    return claim


async def get_review(
    db: AsyncSession,
    principal: Principal,
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    *,
    lock: bool = False,
) -> tuple[HumanReview, Claim]:
    claim = await _claim_for_review(db, principal, claim_id, lock=lock)
    stmt = select(HumanReview).where(HumanReview.id == review_id, HumanReview.claim_id == claim.id)
    review = await db.scalar(stmt.with_for_update() if lock else stmt)
    if review is None:
        raise NotFoundError("Review not found")
    return review, claim


async def for_claim(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> list[HumanReview]:
    claim = await _claim_for_review(db, principal, claim_id)
    stmt = select(HumanReview).where(HumanReview.claim_id == claim.id)
    return list((await db.scalars(stmt.order_by(HumanReview.created_at.desc()))).all())


async def pending_decision(db: AsyncSession, review_id: uuid.UUID) -> PendingDecision | None:
    return await db.scalar(select(PendingDecision).where(PendingDecision.review_id == review_id))


async def decisions(db: AsyncSession, claim_id: uuid.UUID) -> list[ClaimDecision]:
    stmt = select(ClaimDecision).where(ClaimDecision.claim_id == claim_id)
    return list((await db.scalars(stmt.order_by(ClaimDecision.created_at))).all())


async def authorities(db: AsyncSession, user_id: uuid.UUID) -> list[ReviewerAuthority]:
    stmt = select(ReviewerAuthority).where(ReviewerAuthority.user_id == user_id)
    return list((await db.scalars(stmt.order_by(ReviewerAuthority.created_at))).all())


# --- shared checks -------------------------------------------------------------------------------


def _require(principal: Principal, permission: P) -> None:
    if not principal.has(permission.value):
        raise ForbiddenError(f"This action requires the '{permission.value}' permission")


async def _execution(db: AsyncSession, review: HumanReview) -> WorkflowExecution:
    execution = await db.scalar(
        select(WorkflowExecution)
        .where(WorkflowExecution.id == review.execution_id)
        .with_for_update(key_share=True)
    )
    if (
        execution is None
        or execution.status != "interrupted"
        or (execution.interrupt or {}).get("interrupt_id") != review.interrupt_id
    ):
        raise ConflictError("The processing run is not waiting for this review any more")
    return execution


async def _take(db: AsyncSession, principal: Principal, review: HumanReview, claim: Claim) -> None:
    """Make the caller the review owner (and the claim's reviewer) unless someone else holds it."""
    if review.status not in OPEN:
        raise ConflictError(f"This review is {review.status}")
    mine = review.assigned_to == principal.user_id
    if review.status == "in_progress" and not mine and review.assigned_to is not None:
        raise ConflictError("Another reviewer is working on this review")
    if review.status == "in_progress" and mine:
        return
    review.assigned_to, review.status = principal.user_id, "in_progress"
    current = await db.scalar(
        select(ClaimAssignment).where(
            ClaimAssignment.claim_id == claim.id,
            ClaimAssignment.role_on_claim == "reviewer",
            ClaimAssignment.unassigned_at.is_(None),
        )
    )
    if current is not None and current.user_id == principal.user_id:
        return
    if current is not None:
        current.unassigned_at = _now()
        await db.flush()
    db.add(
        ClaimAssignment(
            organization_id=claim.organization_id,
            claim_id=claim.id,
            user_id=principal.user_id,
            role_on_claim="reviewer",
            assigned_by=principal.user_id,
            assigned_at=_now(),
        )
    )


def _touch(review: HumanReview) -> None:
    review.row_version += 1
    review.updated_at = _now()


async def _ruling(
    db: AsyncSession,
    decider: uuid.UUID,
    review: HumanReview,
    claim: Claim,
    action: str,
    amount: Decimal | None,
    override_reason: str | None,
) -> Ruling:
    adjusters = await db.scalars(
        select(ClaimAssignment.user_id).where(
            ClaimAssignment.claim_id == claim.id,
            ClaimAssignment.role_on_claim == "adjuster",
            ClaimAssignment.unassigned_at.is_(None),
        )
    )
    grants = [
        Authority(
            a.max_approval_amount,
            a.currency,
            str(a.product_id) if a.product_id else None,
            can_reject=a.can_reject,
            can_override_fraud=a.can_override_fraud,
            valid_from=a.valid_from,
            valid_to=a.valid_to,
        )
        for a in await authorities(db, decider)
    ]
    config = await _config(db, claim.product_id)
    ctx = DecisionContext(
        action,  # type: ignore[arg-type]
        str(decider),
        amount,
        claim.currency,
        str(claim.product_id),
        review.recommendation,
        review.fraud_level,
        frozenset(str(u) for u in adjusters),
        config.four_eyes_threshold,
        datetime.now(UTC).date(),
        calculated_amount=review.payout,
    )
    return rule(ctx, grants, override_reason=override_reason)


def _deny(ruling: Ruling) -> None:
    if ruling.allowed:
        return
    if any("override reason is required" not in r for r in ruling.reasons):
        raise AuthorityExceededError("; ".join(ruling.reasons))
    raise BusinessRuleError("; ".join(ruling.reasons))


async def _audit(
    db: AsyncSession, actor: AuditActor, action: str, review: HumanReview, after: dict[str, Any]
) -> None:
    await audit_service.record(
        db,
        actor=actor,
        action=action,
        entity_type="claim",
        entity_id=review.claim_id,
        after={"review_id": str(review.id), **after},
    )


def _resume(
    execution: WorkflowExecution, review: HumanReview, action: str, by: uuid.UUID, note: str
) -> Enqueue:
    from app.services import workflow_service  # workflow_service -> graph -> nodes -> this module

    payload = workflow_service.prepare_resume(execution, review.interrupt_id, action, by, note)
    return lambda: workflow_service.enqueue(execution, payload)
