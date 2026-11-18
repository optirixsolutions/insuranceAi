"""Human-review actions (Phase 9): take ownership, approve/reject (with four-eyes), second
approval, request information, return for reassessment, escalate.

Each action locks the review, re-checks the waiting interrupt and the reviewer's authority, writes
its records and audit event, and returns an enqueue callback the API runs after commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthorityExceededError, BusinessRuleError, ConflictError
from app.models.claims import Claim
from app.models.rules import ClaimDecision, HumanReview, PendingDecision
from app.models.workflow import WorkflowExecution
from app.repositories import identity as identity_repo
from app.security.permissions import P
from app.security.principal import Principal
from app.services import events, feedback_service
from app.services.audit_service import AuditActor
from app.services.review_service import (
    OPEN,
    Enqueue,
    _audit,
    _claim_for_review,
    _deny,
    _execution,
    _require,
    _resume,
    _ruling,
    _take,
    _touch,
    get_review,
    pending_decision,
)
from rules_engine.claim_status import ClaimStatus
from rules_engine.review import second_approval
from workflows.runtime import set_claim_status


def _now() -> datetime:
    return datetime.now(UTC)


async def take_ownership(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
) -> HumanReview:
    _require(principal, P.REVIEWS_DECIDE)
    review, claim = await get_review(db, principal, claim_id, review_id, lock=True)
    if review.status == "awaiting_second_approval":
        raise ConflictError("This review is waiting for a second approver")
    await _take(db, principal, review, claim)
    _touch(review)
    await _audit(db, actor, "review.claimed", review, {"assigned_to": str(principal.user_id)})
    return review


@dataclass(frozen=True, slots=True)
class Decision:
    action: str  # approve | reject
    amount: Decimal | None
    reason: str
    reason_code: str | None = None
    override_reason: str | None = None
    clause_ids: tuple[str, ...] = ()


async def _final(
    db: AsyncSession,
    review: HumanReview,
    claim: Claim,
    execution: WorkflowExecution,
    d: Decision,
    *,
    decided_by: uuid.UUID,
    second: uuid.UUID | None,
    override: bool,
) -> ClaimDecision:
    approve = d.action == "approve"
    target = ClaimStatus.APPROVED if approve else ClaimStatus.REJECTED
    decision = ClaimDecision(
        id=uuid.uuid4(),
        organization_id=claim.organization_id,
        claim_id=claim.id,
        execution_id=execution.id,
        decision="approved" if approve else "rejected",
        approved_amount=d.amount if approve else None,
        currency=claim.currency,
        decided_by=decided_by,
        decision_mode="human",
        override=override,
        override_reason=d.override_reason,
        review_id=review.id,
        second_approver_id=second,
        reason=d.reason,
        reason_code=d.reason_code,
        clause_ids=list(d.clause_ids),
        before_state={
            "status": claim.status,
            "recommendation": review.recommendation,
            "payout": str(review.payout) if review.payout is not None else None,
        },
        after_state={
            "status": target.value,
            "approved_amount": str(d.amount) if approve and d.amount is not None else None,
        },
        created_at=_now(),
    )
    db.add(decision)
    await db.flush()
    await set_claim_status(
        db,
        execution,
        target,
        f"human decision: {decision.decision}",
        "decision",
        changed_by=second or decided_by,
    )
    review.status, review.outcome, review.completed_at = "completed", decision.decision, _now()
    _touch(review)
    await events.emit(
        db,
        claim.organization_id,
        "claim.decided",
        events.claim_data(
            claim,
            decision=decision.decision,
            decision_mode="human",
            approved_amount=str(decision.approved_amount) if decision.approved_amount is not None else None,
            currency=decision.currency,
            claim_decision_id=str(decision.id),
        ),
        claim_id=claim.id,
        notices=[
            events.Notice(
                await events.claim_people(db, claim),
                "claim.decided",
                f"{claim.claim_reference} was {decision.decision}",
                ("Decided by a reviewer with a second approver: " if second else "Decided by a reviewer: ")
                + d.reason[:300],
                link=f"/claims/{claim.id}?tab=decision",
            )
        ],
    )
    await feedback_service.from_decision(db, claim, decision.decision, second or decided_by)
    return decision


async def decide(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    d: Decision,
) -> tuple[HumanReview, Enqueue | None]:
    """Approve or reject. Returns the enqueue callback to run after commit (None = awaiting second)."""
    _require(principal, P.REVIEWS_DECIDE)
    if not d.reason.strip():
        raise BusinessRuleError("A decision needs a reason")
    review, claim = await get_review(db, principal, claim_id, review_id, lock=True)
    if review.status == "awaiting_second_approval":
        raise ConflictError("This review already has a first decision waiting for a second approver")
    execution = await _execution(db, review)
    ruling = await _ruling(db, principal.user_id, review, claim, d.action, d.amount, d.override_reason)
    _deny(ruling)
    await _take(db, principal, review, claim)
    if ruling.four_eyes_required:
        db.add(
            PendingDecision(
                id=uuid.uuid4(),
                organization_id=claim.organization_id,
                review_id=review.id,
                decision="approved" if d.action == "approve" else "rejected",
                approved_amount=d.amount if d.action == "approve" else None,
                reason=d.reason,
                reason_code=d.reason_code,
                override=ruling.override,
                override_reason=d.override_reason,
                clause_ids=list(d.clause_ids),
                first_decider=principal.user_id,
                created_at=_now(),
            )
        )
        review.status, review.four_eyes_required = "awaiting_second_approval", True
        _touch(review)
        info = {"decision": d.action, "amount": str(d.amount), "override": ruling.override}
        await _audit(db, actor, "review.first_decision", review, info)
        deciders = await events.holders_of(db, claim.organization_id, "reviews.decide")
        await events.emit(
            db,
            claim.organization_id,
            "review.second_approval_requested",
            events.claim_data(claim, review_id=str(review.id), decision=d.action),
            claim_id=claim.id,
            notices=[
                events.Notice(
                    [u for u in deciders if u != principal.user_id],
                    "review.second_approval",
                    f"Second approval needed for {claim.claim_reference}",
                    f"{principal.display_name} decided to {d.action} this claim; "
                    "a second authorised person must confirm.",
                    link=f"/reviews/{claim.id}/{review.id}",
                    email=True,
                )
            ],
        )
        return review, None
    decision = await _final(
        db, review, claim, execution, d, decided_by=principal.user_id, second=None, override=ruling.override
    )
    info = {"claim_decision_id": str(decision.id), "amount": str(d.amount), "override": ruling.override}
    await _audit(db, actor, f"review.{decision.decision}", review, info)
    return review, _resume(execution, review, d.action, principal.user_id, d.reason)


async def confirm_second(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    *,
    approve: bool,
    note: str,
) -> tuple[HumanReview, Enqueue | None]:
    _require(principal, P.REVIEWS_DECIDE)
    review, claim = await get_review(db, principal, claim_id, review_id, lock=True)
    pending = await pending_decision(db, review.id)
    if review.status != "awaiting_second_approval" or pending is None:
        raise ConflictError("This review is not waiting for a second approval")
    action = "approve" if pending.decision == "approved" else "reject"
    ruling = await _ruling(
        db, principal.user_id, review, claim, action, pending.approved_amount, pending.override_reason
    )
    refused = second_approval(str(pending.first_decider), str(principal.user_id), ruling)
    if refused:
        raise AuthorityExceededError("; ".join(refused))
    first = pending.first_decider
    if not approve:  # the second person disagrees: back to the first decider's desk
        await db.delete(pending)
        review.status, review.assigned_to = "in_progress", first
        _touch(review)
        await _audit(db, actor, "review.second_approval_declined", review, {"note": note[:500]})
        return review, None
    execution = await _execution(db, review)
    d = Decision(
        action,
        pending.approved_amount,
        pending.reason,
        pending.reason_code,
        pending.override_reason,
        tuple(pending.clause_ids),
    )
    decision = await _final(
        db, review, claim, execution, d, decided_by=first, second=principal.user_id, override=pending.override
    )
    await db.delete(pending)
    info = {"claim_decision_id": str(decision.id), "first_decider": str(first)}
    await _audit(db, actor, "review.second_approval", review, info)
    return review, _resume(execution, review, action, first, d.reason)


async def route_back(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    *,
    action: str,  # request_information | return
    reason: str,
    override_reason: str | None,
    detail: dict[str, Any],
) -> tuple[HumanReview, Enqueue]:
    """Ask for information or return for reassessment; the workflow takes it from there."""
    _require(principal, P.REVIEWS_DECIDE)
    if not reason.strip():
        raise BusinessRuleError("A reason is required")
    review, claim = await get_review(db, principal, claim_id, review_id, lock=True)
    if review.status == "awaiting_second_approval":
        raise ConflictError("This review is waiting for a second approver")
    execution = await _execution(db, review)
    resume_action = "reassess" if action == "return" else action
    if resume_action not in (execution.interrupt or {}).get("actions", []):
        raise BusinessRuleError("The reassessment limit for this claim has been reached")
    ruling = await _ruling(db, principal.user_id, review, claim, action, None, override_reason)
    _deny(ruling)
    await _take(db, principal, review, claim)
    kind = "more_information" if action == "request_information" else "returned"
    db.add(
        ClaimDecision(
            id=uuid.uuid4(),
            organization_id=claim.organization_id,
            claim_id=claim.id,
            execution_id=execution.id,
            decision=kind,
            currency=claim.currency,
            decided_by=principal.user_id,
            decision_mode="human",
            override=ruling.override,
            override_reason=override_reason,
            review_id=review.id,
            reason=reason,
            before_state={"status": claim.status, "recommendation": review.recommendation},
            after_state=detail,
            created_at=_now(),
        )
    )
    review.status, review.outcome, review.completed_at = "completed", kind, _now()
    _touch(review)
    await _audit(db, actor, f"review.{kind}", review, {"reason": reason[:500], **detail})
    return review, _resume(execution, review, resume_action, principal.user_id, reason)


async def escalate(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    *,
    to_user_id: uuid.UUID | None,
    reason: str,
) -> HumanReview:
    """Hand the review to another decider (or back to the shared queue); the claim stays in review."""
    _require(principal, P.REVIEWS_ESCALATE)
    review, _claim = await get_review(db, principal, claim_id, review_id, lock=True)
    if review.status not in OPEN or review.status == "awaiting_second_approval":
        raise ConflictError(f"This review cannot be escalated while {review.status}")
    if not reason.strip():
        raise BusinessRuleError("An escalation needs a reason")
    if to_user_id is not None:
        if to_user_id == principal.user_id:
            raise BusinessRuleError("Escalate to someone else")
        user = await identity_repo.get_user(db, principal.organization_id, to_user_id)
        perms = (
            await identity_repo.user_permissions(db, principal.organization_id, to_user_id) if user else set()
        )
        if user is None or user.status != "active" or P.REVIEWS_DECIDE.value not in perms:
            raise BusinessRuleError("Escalate to an active user who can decide reviews")
    before = str(review.assigned_to) if review.assigned_to else None
    review.assigned_to, review.status = to_user_id, "escalated"
    review.priority_score += 20
    _touch(review)
    org = _claim.organization_id
    targets = [to_user_id] if to_user_id else await events.holders_of(db, org, "reviews.decide")
    await events.emit(
        db,
        org,
        "review.escalated",
        events.claim_data(
            _claim, review_id=str(review.id), assigned_to=str(to_user_id) if to_user_id else None
        ),
        claim_id=_claim.id,
        notices=[
            events.Notice(
                [u for u in targets if u != principal.user_id],
                "review.escalated",
                f"{_claim.claim_reference} was escalated" + (" to you" if to_user_id else ""),
                f"{principal.display_name}: {reason[:300]}",
                link=f"/reviews/{_claim.id}/{review.id}",
                email=to_user_id is not None,
            )
        ],
    )
    info = {"from": before, "to": str(to_user_id) if to_user_id else None, "reason": reason[:500]}
    await _audit(db, actor, "review.escalated", review, info)
    return review


async def assign_open(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    claim_id: uuid.UUID,
    assignee: uuid.UUID | None,
    reason: str,
) -> HumanReview:
    """POST /claims/{id}/reviews: route the claim's open review to a person (manual path)."""
    _require(principal, P.REVIEWS_ESCALATE)
    claim = await _claim_for_review(db, principal, claim_id)
    review = await db.scalar(
        select(HumanReview).where(HumanReview.claim_id == claim.id, HumanReview.status.in_(OPEN))
    )
    if review is None:
        raise ConflictError(
            "This claim is not waiting for human review; start or resume its processing first"
        )
    return await escalate(db, principal, actor, claim.id, review.id, to_user_id=assignee, reason=reason)
