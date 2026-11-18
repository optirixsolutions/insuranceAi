"""Claims: intake, listing, detail, edits, status, assignment, notes, timeline, export."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ActorDep, DbDep, etag, parse_if_match, require
from app.core.errors import BusinessRuleError
from app.models.catalog import Customer, InsuranceProduct, Policy, PolicyVersion
from app.models.claims import Claim
from app.models.identity import User
from app.schemas.claims import (
    AssigneeOut,
    AssignmentOut,
    AssignmentRequest,
    ClaimCreate,
    ClaimCreated,
    ClaimDetail,
    ClaimListItem,
    ClaimUpdate,
    Finding,
    LossTypesOut,
    MoneyOut,
    NoteCreate,
    NoteOut,
    PolicyVersionRef,
    RelatedClaimOut,
    StatusChangeRequest,
    TimelineEntryOut,
)
from app.schemas.common import Page
from app.security.permissions import P
from app.security.principal import Principal
from app.services import claim_activity_service as activity
from app.services import claim_service, document_service, idempotency
from app.services.claim_access import sees_all_claims
from app.services.claim_service import ClaimFilters, NewClaim
from rules_engine.claim_status import ClaimStatus
from rules_engine.intake import LOSS_TYPES

router = APIRouter(tags=["claims"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Writer = Annotated[Principal, Depends(require(P.CLAIMS_WRITE))]
Assigner = Annotated[Principal, Depends(require(P.CLAIMS_ASSIGN))]


def _item(
    claim: Claim, customer: Customer, policy: Policy, product: InsuranceProduct, adjuster: User | None
) -> ClaimListItem:
    return ClaimListItem(
        id=claim.id,
        claim_reference=claim.claim_reference,
        status=claim.status,
        priority=claim.priority,
        loss_type=claim.category,
        incident_date=claim.incident_date,
        claimed_amount=MoneyOut(amount=claim.claimed_amount, currency=claim.currency),
        customer_id=customer.id,
        customer_name=customer.full_name,
        policy_id=policy.id,
        policy_number=policy.policy_number,
        product_name=product.name,
        product_line=product.line,
        adjuster_name=adjuster.display_name if adjuster else None,
        is_synthetic=claim.is_synthetic,
        created_at=claim.created_at,
    )


async def _detail(db: AsyncSession, principal: Principal, claim: Claim) -> ClaimDetail:
    customer = await db.get(Customer, claim.customer_id)
    policy = await db.get(Policy, claim.policy_id)
    product = await db.get(InsuranceProduct, claim.product_id)
    assert customer and policy and product
    version = await db.get(PolicyVersion, claim.policy_version_id) if claim.policy_version_id else None
    assigned = await activity.assignments(db, claim.id)
    adjuster = next((a for a in assigned if a.role_on_claim == "adjuster"), None)
    base = _item(claim, customer, policy, product, None).model_dump()
    base["adjuster_name"] = adjuster.display_name if adjuster else None
    can_edit = principal.has(P.CLAIMS_WRITE.value) and (
        principal.has(P.CLAIMS_ASSIGN.value) or any(a.user_id == principal.user_id for a in assigned)
    )
    return ClaimDetail(
        **base,
        incident_time=claim.incident_time,
        incident_location=claim.incident_location,
        description=claim.description,
        channel=claim.channel,
        reported_at=claim.reported_at,
        policy_version=PolicyVersionRef(
            id=version.id,
            version_no=version.version_no,
            effective_from=version.effective_from,
            effective_to=version.effective_to,
            sum_insured=version.sum_insured,
            deductible_amount=version.deductible_amount,
            currency=version.currency,
        )
        if version
        else None,
        findings=[Finding(**f) for f in claim.intake_findings],
        assignments=[
            AssignmentOut(
                role_on_claim=a.role_on_claim,
                user_id=a.user_id,
                display_name=a.display_name,
                assigned_at=a.assigned_at,
            )
            for a in assigned
        ],
        allowed_status_changes=claim_service.manual_targets(claim) if can_edit else [],
        required_documents=await claim_service.required_documents(db, claim.product_id),
        documents_received=sorted(await document_service.received_types(db, claim.id)),
        row_version=claim.row_version,
        can_edit=can_edit,
    )


def _filters(
    statuses: list[str] | None,
    priority: str | None,
    product_id: uuid.UUID | None,
    assignee: str | None,
    q: str | None,
    incident_from: date | None,
    incident_to: date | None,
    is_synthetic: bool | None,
    sort: str,
    principal: Principal,
) -> ClaimFilters:
    if not claim_service.sort_key_valid(sort):
        raise BusinessRuleError("Unsupported sort key")
    valid = {s.value for s in ClaimStatus}
    if statuses and not set(statuses) <= valid:
        raise BusinessRuleError("Unknown claim status in filter")
    assignee_id: uuid.UUID | None = None
    unassigned = False
    if assignee == "me":
        assignee_id = principal.user_id
    elif assignee == "none":
        unassigned = True
    elif assignee:
        try:
            assignee_id = uuid.UUID(assignee)
        except ValueError as exc:
            raise BusinessRuleError("assignee must be 'me', 'none' or a user id") from exc
    return ClaimFilters(
        statuses=statuses or [],
        priority=priority,
        product_id=product_id,
        assignee_id=assignee_id,
        unassigned=unassigned,
        q=q,
        incident_from=incident_from,
        incident_to=incident_to,
        is_synthetic=is_synthetic,
        sort=sort,
    )


@router.get("/claims", response_model=Page[ClaimListItem])
async def list_claims(
    principal: Reader,
    db: DbDep,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    priority: Literal["low", "normal", "high", "urgent"] | None = None,
    product_id: uuid.UUID | None = None,
    assignee: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    incident_from: date | None = None,
    incident_to: date | None = None,
    is_synthetic: bool | None = None,
    sort: str = "-created_at",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[ClaimListItem]:
    filters = _filters(
        status_filter,
        priority,
        product_id,
        assignee,
        q,
        incident_from,
        incident_to,
        is_synthetic,
        sort,
        principal,
    )
    rows, total = await claim_service.list_claims(db, principal, filters, page=page, page_size=page_size)
    return Page(items=[_item(*r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/claims/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV of the filtered claims"}},
)
async def export_claims(
    principal: Reader,
    db: DbDep,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    priority: Literal["low", "normal", "high", "urgent"] | None = None,
    product_id: uuid.UUID | None = None,
    assignee: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    incident_from: date | None = None,
    incident_to: date | None = None,
    is_synthetic: bool | None = None,
    sort: str = "-created_at",
) -> Response:
    filters = _filters(
        status_filter,
        priority,
        product_id,
        assignee,
        q,
        incident_from,
        incident_to,
        is_synthetic,
        sort,
        principal,
    )
    content = await activity.export_csv(db, principal, filters)
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="claims.csv"'},
    )


@router.get("/claims/loss-types", response_model=LossTypesOut)
async def loss_types(_: Reader) -> LossTypesOut:
    return LossTypesOut(lines={line: list(types) for line, types in LOSS_TYPES.items()})


@router.get("/claims/assignees", response_model=list[AssigneeOut])
async def assignees(
    principal: Assigner, db: DbDep, role: Literal["adjuster", "reviewer", "investigator"] = "adjuster"
) -> list[AssigneeOut]:
    users = await activity.assignable_users(db, principal, role)
    return [AssigneeOut(id=u.id, display_name=u.display_name, email=u.email) for u in users]


@router.post("/claims", response_model=ClaimCreated, status_code=status.HTTP_201_CREATED)
async def create_claim(
    body: ClaimCreate,
    request: Request,
    principal: Writer,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> ClaimCreated | JSONResponse:
    """Submit a claim. Requires an Idempotency-Key header; retries replay the original response."""
    key = idempotency.validate_key(idempotency_key)
    digest = idempotency.request_hash(body.model_dump(mode="json"))
    path = request.url.path
    replay = await idempotency.find_replay(db, principal, key, "POST", path, digest)
    if replay is not None:
        return JSONResponse(replay[1], status_code=replay[0], headers={"Idempotent-Replayed": "true"})

    loc = body.incident_location.model_dump(exclude_none=True) if body.incident_location else None
    claim, intake = await claim_service.create_claim(
        db,
        principal,
        actor,
        NewClaim(
            customer_id=body.customer_id,
            policy_id=body.policy_id,
            loss_type=body.loss_type,
            incident_date=body.incident_date,
            incident_time=body.incident_time,
            incident_location=loc,
            description=body.description,
            claimed_amount=body.claimed_amount.amount,
            currency=body.claimed_amount.currency,
            channel=body.channel,
            is_synthetic=body.is_synthetic,
        ),
    )
    detail = await _detail(db, principal, claim)
    created = ClaimCreated(
        **detail.model_dump(),
        missing_documents=intake.missing_documents,
        duplicate_suspects=intake.duplicates,
    )
    idempotency.remember(db, principal, key, "POST", path, digest, 201, created.model_dump(mode="json"))
    try:
        await db.commit()
    except IntegrityError:  # a concurrent request with the same key won the race
        await db.rollback()
        replay = await idempotency.find_replay(db, principal, key, "POST", path, digest)
        if replay is None:
            raise
        return JSONResponse(replay[1], status_code=replay[0], headers={"Idempotent-Replayed": "true"})
    return created


@router.get("/claims/{claim_id}", response_model=ClaimDetail)
async def get_claim(claim_id: uuid.UUID, principal: Reader, db: DbDep, response: Response) -> ClaimDetail:
    claim = await claim_service.get_claim(db, principal, claim_id)
    response.headers["ETag"] = etag(claim.row_version)
    return await _detail(db, principal, claim)


@router.patch("/claims/{claim_id}", response_model=ClaimDetail)
async def update_claim(
    claim_id: uuid.UUID,
    body: ClaimUpdate,
    principal: Writer,
    actor: ActorDep,
    db: DbDep,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> ClaimDetail:
    changes = body.model_dump(exclude_unset=True)
    if "loss_type" in changes:
        changes["category"] = changes.pop("loss_type")
    if changes.get("incident_location") is not None:
        changes["incident_location"] = {
            k: v for k, v in changes["incident_location"].items() if v is not None
        }
    claim = await claim_service.update_claim(
        db, principal, actor, claim_id, expected_version=parse_if_match(if_match), changes=changes
    )
    response.headers["ETag"] = etag(claim.row_version)
    return await _detail(db, principal, claim)


@router.post("/claims/{claim_id}/status", response_model=ClaimDetail)
async def change_status(
    claim_id: uuid.UUID,
    body: StatusChangeRequest,
    principal: Writer,
    actor: ActorDep,
    db: DbDep,
    if_match: Annotated[str | None, Header()] = None,
) -> ClaimDetail:
    claim = await claim_service.change_status(
        db,
        principal,
        actor,
        claim_id,
        target=body.status,
        reason=body.reason,
        expected_version=parse_if_match(if_match),
    )
    return await _detail(db, principal, claim)


@router.put("/claims/{claim_id}/assignments", response_model=list[AssignmentOut])
async def assign(
    claim_id: uuid.UUID, body: AssignmentRequest, principal: Assigner, actor: ActorDep, db: DbDep
) -> list[AssignmentOut]:
    result = await activity.assign(db, principal, actor, claim_id, role=body.role, user_id=body.user_id)
    return [
        AssignmentOut(
            role_on_claim=a.role_on_claim,
            user_id=a.user_id,
            display_name=a.display_name,
            assigned_at=a.assigned_at,
        )
        for a in result
    ]


@router.get("/claims/{claim_id}/notes", response_model=list[NoteOut])
async def list_notes(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[NoteOut]:
    return [
        NoteOut(
            id=n.id,
            body=n.body,
            visibility=n.visibility,
            author_id=n.author_id,
            author_name=name,
            created_at=n.created_at,
        )
        for n, name in await activity.list_notes(db, principal, claim_id)
    ]


@router.post("/claims/{claim_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def add_note(
    claim_id: uuid.UUID, body: NoteCreate, principal: Reader, actor: ActorDep, db: DbDep
) -> NoteOut:
    note = await activity.add_note(db, principal, actor, claim_id, body=body.body, visibility=body.visibility)
    return NoteOut(
        id=note.id,
        body=note.body,
        visibility=note.visibility,
        author_id=note.author_id,
        author_name=principal.display_name,
        created_at=note.created_at,
    )


@router.delete("/claims/{claim_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    claim_id: uuid.UUID, note_id: uuid.UUID, principal: Reader, actor: ActorDep, db: DbDep
) -> None:
    await activity.delete_note(db, principal, actor, claim_id, note_id)


@router.get("/claims/{claim_id}/timeline", response_model=list[TimelineEntryOut])
async def timeline(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[TimelineEntryOut]:
    return [
        TimelineEntryOut(at=e.at, kind=e.kind, title=e.title, detail=e.detail, actor_name=e.actor_name)
        for e in await activity.timeline(db, principal, claim_id)
    ]


@router.get("/claims/{claim_id}/related", response_model=list[RelatedClaimOut])
async def related(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[RelatedClaimOut]:
    out = []
    for rel, other in await activity.related(db, principal, claim_id):
        # Only users with organisation-wide visibility see links to claims outside their assignments.
        if not sees_all_claims(principal):
            continue
        out.append(
            RelatedClaimOut(
                id=other.id,
                claim_reference=other.claim_reference,
                status=other.status,
                incident_date=other.incident_date,
                relation=rel.relation,
                source=rel.source,
                detail=rel.detail,
            )
        )
    return out
