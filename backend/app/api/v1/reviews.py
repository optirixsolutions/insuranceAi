"""Human review (05-api-specification.md §2.8) and reviewer authorities.

Decision endpoints (approve, reject, request-information, return, second-approval) require an
Idempotency-Key; the stored response is written in the same transaction as the decision, and the
workflow resume is enqueued only after that commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import ActorDep, DbDep, require
from app.api.v1.rules import DecisionOut
from app.core.errors import NotFoundError
from app.models.claims import Claim
from app.models.identity import User
from app.models.rules import HumanReview
from app.models.workflow import WorkflowExecution
from app.security.permissions import P
from app.security.principal import Principal
from app.services import authority_service, idempotency, review_actions, review_service
from app.services.claim_activity_service import assignable_users
from app.services.review_service import Enqueue

router = APIRouter(tags=["reviews"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Manager = Annotated[Principal, Depends(require(P.USERS_MANAGE))]
Escalator = Annotated[Principal, Depends(require(P.REVIEWS_ESCALATE))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]
Money = Annotated[Decimal, Field(ge=0, max_digits=19, decimal_places=4)]
Reason = Annotated[str, Field(min_length=1, max_length=1000)]
OptText = Annotated[str | None, Field(max_length=1000)]
Act = Callable[[], Awaitable[tuple[HumanReview, Enqueue | None]]]


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_id: uuid.UUID
    execution_id: uuid.UUID
    recommendation: str | None
    payout: Decimal | None
    currency: str
    fraud_level: str | None
    reasons: list[str]
    assigned_to: uuid.UUID | None
    status: str
    priority_score: int
    due_at: datetime | None
    four_eyes_required: bool
    outcome: str | None
    created_at: datetime
    completed_at: datetime | None
    row_version: int


class QueueItem(ReviewOut):
    claim_reference: str
    claim_status: str
    claimed_amount: Decimal
    product_id: uuid.UUID
    assigned_to_name: str | None
    overdue: bool


class PendingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    decision: str
    approved_amount: Decimal | None
    reason: str
    reason_code: str | None
    override: bool
    override_reason: str | None
    clause_ids: list[str]
    first_decider: uuid.UUID
    created_at: datetime


class AuthorityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    max_approval_amount: Decimal
    currency: str
    product_id: uuid.UUID | None
    can_reject: bool
    can_override_fraud: bool
    valid_from: date
    valid_to: date | None


class Workspace(BaseModel):
    review: QueueItem
    claim_description: str
    recommendation_explanation: str | None
    matrix_rule: str | None
    contradictions: list[str]
    actions: list[str]  # what the waiting interrupt accepts ("reassess" = return)
    waiting: bool  # the processing run is waiting for this review
    pending_decision: PendingOut | None
    first_decider_name: str | None
    decisions: list[DecisionOut]
    my_authorities: list[AuthorityOut]


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_amount: Money
    reason: Reason
    override_reason: OptText = None


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: Annotated[str, Field(min_length=1, max_length=60)]
    reason: Reason
    clause_ids: Annotated[list[Annotated[str, Field(max_length=80)]], Field(max_length=20)] = []
    override_reason: OptText = None


class InfoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=300)]], Field(min_length=1, max_length=30)
    ]
    message: Reason
    override_reason: OptText = None


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason
    instructions: Annotated[str, Field(max_length=2000)] = ""
    override_reason: OptText = None


class EscalateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_user_id: uuid.UUID | None = None
    reason: Reason


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee_id: uuid.UUID | None = None
    reason: Reason = "assigned for review"


class SecondIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool = True
    note: Annotated[str, Field(max_length=1000)] = ""


class AuthorityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_approval_amount: Money
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")]
    product_id: uuid.UUID | None = None
    can_reject: bool = False
    can_override_fraud: bool = False
    valid_from: date
    valid_to: date | None = None


# --- helpers -------------------------------------------------------------------------------------


async def _names(db: DbDep, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await db.execute(select(User.id, User.display_name).where(User.id.in_(wanted)))
    return dict(rows.tuples().all())


def _item(review: HumanReview, claim: Claim, names: dict[uuid.UUID, str]) -> QueueItem:
    now = datetime.now(review.created_at.tzinfo)
    late = review.status in review_service.OPEN and review.due_at is not None and review.due_at < now
    return QueueItem(
        **ReviewOut.model_validate(review).model_dump(),
        claim_reference=claim.claim_reference,
        claim_status=claim.status,
        claimed_amount=claim.claimed_amount,
        product_id=claim.product_id,
        assigned_to_name=names.get(review.assigned_to) if review.assigned_to else None,
        overdue=late,
    )


async def _workspace(db: DbDep, principal: Principal, review: HumanReview, claim: Claim) -> Workspace:
    pending = await review_service.pending_decision(db, review.id)
    names = await _names(db, {review.assigned_to, pending.first_decider if pending else None})
    execution = await db.get(WorkflowExecution, review.execution_id)
    interrupt: dict[str, Any] = (execution.interrupt if execution else None) or {}
    waiting = (
        execution is not None
        and execution.status == "interrupted"
        and interrupt.get("interrupt_id") == review.interrupt_id
    )
    mine = await review_service.authorities(db, principal.user_id)
    return Workspace(
        review=_item(review, claim, names),
        claim_description=claim.description[:4000],
        recommendation_explanation=interrupt.get("explanation"),
        matrix_rule=interrupt.get("matrix_rule"),
        contradictions=[str(c) for c in interrupt.get("contradictions", [])],
        actions=list(interrupt.get("actions", [])) if waiting else [],
        waiting=waiting,
        pending_decision=PendingOut.model_validate(pending) if pending else None,
        first_decider_name=names.get(pending.first_decider) if pending else None,
        decisions=[DecisionOut.model_validate(d) for d in await review_service.decisions(db, claim.id)],
        my_authorities=[AuthorityOut.model_validate(a) for a in mine],
    )


async def _idempotent(
    request: Request, db: DbDep, principal: Principal, key: str | None, body: BaseModel, act: Act
) -> ReviewOut | JSONResponse:
    key = idempotency.validate_key(key)
    digest = idempotency.request_hash(body.model_dump(mode="json"))
    path = request.url.path
    replay = await idempotency.find_replay(db, principal, key, "POST", path, digest)
    if replay is not None:
        return JSONResponse(replay[1], status_code=replay[0], headers={"Idempotent-Replayed": "true"})
    review, enqueue = await act()
    out = ReviewOut.model_validate(review)
    idempotency.remember(db, principal, key, "POST", path, digest, 200, out.model_dump(mode="json"))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        replay = await idempotency.find_replay(db, principal, key, "POST", path, digest)
        if replay is None:
            raise
        return JSONResponse(replay[1], status_code=replay[0], headers={"Idempotent-Replayed": "true"})
    if enqueue is not None:
        enqueue()
    return out


# --- queue and workspace -------------------------------------------------------------------------


@router.get("/reviews", response_model=list[QueueItem])
async def review_queue(
    principal: Reader,
    db: DbDep,
    status: Annotated[str | None, Query(max_length=30)] = None,
    assignee: Annotated[str | None, Query(pattern="^me$")] = None,
    risk_level: Annotated[str | None, Query(max_length=20)] = None,
    product_id: uuid.UUID | None = None,
    overdue: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[QueueItem]:
    filters = review_service.QueueFilters(status, assignee == "me", risk_level, product_id, overdue, limit)
    rows = await review_service.queue(db, principal, filters)
    names = await _names(db, {r.assigned_to for r, _ in rows})
    return [_item(r, c, names) for r, c in rows]


class DeciderOut(BaseModel):
    id: uuid.UUID
    display_name: str


@router.get("/reviews/deciders", response_model=list[DeciderOut])
async def deciders(principal: Escalator, db: DbDep) -> list[DeciderOut]:
    """People a review can be escalated to (active users holding ``reviews.decide``)."""
    users = await assignable_users(db, principal, "reviewer")
    return [DeciderOut(id=u.id, display_name=u.display_name) for u in users if u.id != principal.user_id]


@router.post("/claims/{claim_id}/reviews", response_model=ReviewOut)
async def assign_review(
    claim_id: uuid.UUID, body: AssignIn, principal: Reader, actor: ActorDep, db: DbDep
) -> HumanReview:
    review = await review_actions.assign_open(db, principal, actor, claim_id, body.assignee_id, body.reason)
    await db.commit()
    return review


@router.get("/claims/{claim_id}/reviews", response_model=list[ReviewOut])
async def claim_reviews(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[HumanReview]:
    return await review_service.for_claim(db, principal, claim_id)


@router.get("/claims/{claim_id}/reviews/{review_id}", response_model=Workspace)
async def review_workspace(
    claim_id: uuid.UUID, review_id: uuid.UUID, principal: Reader, db: DbDep
) -> Workspace:
    review, claim = await review_service.get_review(db, principal, claim_id, review_id)
    return await _workspace(db, principal, review, claim)


@router.post("/claims/{claim_id}/reviews/{review_id}/claim", response_model=ReviewOut)
async def claim_review(
    claim_id: uuid.UUID, review_id: uuid.UUID, principal: Reader, actor: ActorDep, db: DbDep
) -> HumanReview:
    review = await review_actions.take_ownership(db, principal, actor, claim_id, review_id)
    await db.commit()
    return review


@router.post("/claims/{claim_id}/reviews/{review_id}/escalate", response_model=ReviewOut)
async def escalate_review(
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    body: EscalateIn,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
) -> HumanReview:
    review = await review_actions.escalate(
        db, principal, actor, claim_id, review_id, to_user_id=body.to_user_id, reason=body.reason
    )
    await db.commit()
    return review


# --- decisions (idempotent) ----------------------------------------------------------------------


@router.post("/claims/{claim_id}/reviews/{review_id}/approve", response_model=ReviewOut)
async def approve(
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    body: ApproveIn,
    request: Request,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: IdemKey = None,
) -> ReviewOut | JSONResponse:
    d = review_actions.Decision("approve", body.approved_amount, body.reason, None, body.override_reason)

    async def act() -> tuple[HumanReview, Enqueue | None]:
        return await review_actions.decide(db, principal, actor, claim_id, review_id, d)

    return await _idempotent(request, db, principal, idempotency_key, body, act)


@router.post("/claims/{claim_id}/reviews/{review_id}/reject", response_model=ReviewOut)
async def reject(
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    body: RejectIn,
    request: Request,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: IdemKey = None,
) -> ReviewOut | JSONResponse:
    d = review_actions.Decision(
        "reject", None, body.reason, body.reason_code, body.override_reason, tuple(body.clause_ids)
    )

    async def act() -> tuple[HumanReview, Enqueue | None]:
        return await review_actions.decide(db, principal, actor, claim_id, review_id, d)

    return await _idempotent(request, db, principal, idempotency_key, body, act)


@router.post("/claims/{claim_id}/reviews/{review_id}/request-information", response_model=ReviewOut)
async def request_information(
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    body: InfoIn,
    request: Request,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: IdemKey = None,
) -> ReviewOut | JSONResponse:
    async def act() -> tuple[HumanReview, Enqueue | None]:
        return await review_actions.route_back(
            db,
            principal,
            actor,
            claim_id,
            review_id,
            action="request_information",
            reason=body.message,
            override_reason=body.override_reason,
            detail={"items": body.items},
        )

    return await _idempotent(request, db, principal, idempotency_key, body, act)


@router.post("/claims/{claim_id}/reviews/{review_id}/return", response_model=ReviewOut)
async def return_for_reassessment(
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    body: ReturnIn,
    request: Request,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: IdemKey = None,
) -> ReviewOut | JSONResponse:
    async def act() -> tuple[HumanReview, Enqueue | None]:
        return await review_actions.route_back(
            db,
            principal,
            actor,
            claim_id,
            review_id,
            action="return",
            reason=body.reason,
            override_reason=body.override_reason,
            detail={"instructions": body.instructions},
        )

    return await _idempotent(request, db, principal, idempotency_key, body, act)


@router.post("/claims/{claim_id}/reviews/{review_id}/second-approval", response_model=ReviewOut)
async def second_approval(
    claim_id: uuid.UUID,
    review_id: uuid.UUID,
    body: SecondIn,
    request: Request,
    principal: Reader,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: IdemKey = None,
) -> ReviewOut | JSONResponse:
    async def act() -> tuple[HumanReview, Enqueue | None]:
        return await review_actions.confirm_second(
            db, principal, actor, claim_id, review_id, approve=body.approve, note=body.note
        )

    return await _idempotent(request, db, principal, idempotency_key, body, act)


# --- authorities ---------------------------------------------------------------------------------


@router.get("/users/{user_id}/authorities", response_model=list[AuthorityOut])
async def get_authorities(user_id: uuid.UUID, principal: Reader, db: DbDep) -> list[Any]:
    if user_id != principal.user_id and not principal.has(P.USERS_MANAGE.value):
        raise NotFoundError("User not found")  # others' authorities: user managers only
    return list(await review_service.authorities(db, user_id))


@router.put("/users/{user_id}/authorities", response_model=list[AuthorityOut])
async def put_authorities(
    user_id: uuid.UUID,
    body: Annotated[list[AuthorityIn], Body(max_length=20)],
    principal: Manager,
    actor: ActorDep,
    db: DbDep,
) -> list[Any]:
    items = [authority_service.AuthorityInput(**i.model_dump()) for i in body]
    rows = await authority_service.replace_authorities(db, principal, actor, user_id, items)
    await db.commit()
    return rows
