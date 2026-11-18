"""Rule sets (versioned product configuration) and claim decisions."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import ActorDep, DbDep, parse_if_match, require
from app.models.rules import ClaimDecision, RuleSet
from app.security.permissions import P
from app.security.principal import Principal
from app.services import rule_set_service
from app.services.claim_access import get_visible_claim
from rules_engine.rule_set import DEFAULT_CONFIG, RuleSetConfig

router = APIRouter(tags=["rules"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Editor = Annotated[Principal, Depends(require(P.PRODUCTS_MANAGE))]
Publisher = Annotated[Principal, Depends(require(P.RULES_PUBLISH))]
IfMatch = Annotated[str | None, Header()]


class RuleSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    version_no: int
    status: str
    config: dict[str, Any]
    config_sha256: str
    change_note: str
    created_by: uuid.UUID | None
    created_at: datetime
    published_by: uuid.UUID | None
    published_at: datetime | None
    row_version: int


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any] | None = None  # None = copy the published version (or the defaults)
    change_note: Annotated[str, Field(max_length=500)] = ""


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    change_note: Annotated[str, Field(max_length=500)] | None = None


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_id: uuid.UUID
    execution_id: uuid.UUID | None
    decision: str
    approved_amount: Decimal | None
    currency: str
    decided_by: uuid.UUID | None
    decision_mode: str
    override: bool
    override_reason: str | None
    review_id: uuid.UUID | None
    second_approver_id: uuid.UUID | None
    reason: str | None
    reason_code: str | None
    clause_ids: list[str]
    rule_set_id: uuid.UUID | None
    gates: list[dict[str, Any]]
    created_at: datetime


@router.get("/rule-sets/defaults")
async def rule_set_defaults(principal: Reader) -> dict[str, Any]:
    """The built-in configuration (used until a product publishes its own) and its JSON schema."""
    return {"config": DEFAULT_CONFIG.model_dump(mode="json"), "schema": RuleSetConfig.model_json_schema()}


@router.get("/products/{product_id}/rule-sets", response_model=list[RuleSetOut])
async def list_rule_sets(product_id: uuid.UUID, principal: Reader, db: DbDep) -> list[RuleSet]:
    return await rule_set_service.list_versions(db, principal, product_id)


@router.post(
    "/products/{product_id}/rule-sets", response_model=RuleSetOut, status_code=status.HTTP_201_CREATED
)
async def create_rule_set(
    product_id: uuid.UUID, body: DraftIn, principal: Editor, actor: ActorDep, db: DbDep
) -> RuleSet:
    return await rule_set_service.create_draft(
        db, principal, product_id, actor, body.config, body.change_note
    )


@router.get("/rule-sets/{rule_set_id}", response_model=RuleSetOut)
async def get_rule_set(rule_set_id: uuid.UUID, principal: Reader, db: DbDep) -> RuleSet:
    return await rule_set_service.get(db, principal, rule_set_id)


@router.patch("/rule-sets/{rule_set_id}", response_model=RuleSetOut)
async def update_rule_set(
    rule_set_id: uuid.UUID,
    body: DraftUpdate,
    principal: Editor,
    actor: ActorDep,
    db: DbDep,
    if_match: IfMatch = None,
) -> RuleSet:
    return await rule_set_service.update_draft(
        db, principal, rule_set_id, actor, body.config, body.change_note, parse_if_match(if_match)
    )


@router.post("/rule-sets/{rule_set_id}/publish", response_model=RuleSetOut)
async def publish_rule_set(
    rule_set_id: uuid.UUID, principal: Publisher, actor: ActorDep, db: DbDep, if_match: IfMatch = None
) -> RuleSet:
    """Publish a draft; the previous published version is retired (never edited or deleted)."""
    return await rule_set_service.publish(db, principal, rule_set_id, actor, parse_if_match(if_match))


@router.get("/claims/{claim_id}/decisions", response_model=list[DecisionOut])
async def list_decisions(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[ClaimDecision]:
    claim = await get_visible_claim(db, principal, claim_id)
    rows = await db.scalars(
        select(ClaimDecision)
        .where(ClaimDecision.claim_id == claim.id)
        .order_by(ClaimDecision.created_at.desc())
    )
    return list(rows.all())
