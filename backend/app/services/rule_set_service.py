"""Rule-set versions per product: draft, edit, publish (retiring the previous), read the active one."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models.catalog import InsuranceProduct
from app.models.rules import RuleSet
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from rules_engine.rule_set import DEFAULT_CONFIG, RuleSetConfig


def _sha(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return RuleSetConfig.model_validate(config).model_dump(mode="json")
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise BusinessRuleError(f"Invalid rule set: {problems}") from exc


async def _product(db: AsyncSession, principal: Principal, product_id: uuid.UUID) -> InsuranceProduct:
    product = await db.get(InsuranceProduct, product_id)
    if product is None or product.organization_id != principal.organization_id:
        raise NotFoundError("Product not found")
    return product


async def list_versions(db: AsyncSession, principal: Principal, product_id: uuid.UUID) -> list[RuleSet]:
    product = await _product(db, principal, product_id)
    rows = await db.scalars(
        select(RuleSet).where(RuleSet.product_id == product.id).order_by(RuleSet.version_no.desc())
    )
    return list(rows.all())


async def get(
    db: AsyncSession, principal: Principal, rule_set_id: uuid.UUID, *, lock: bool = False
) -> RuleSet:
    # lock: a concurrent publish and edit serialise here, so the loser sees a 409, not the trigger
    row = await db.get(RuleSet, rule_set_id, with_for_update=lock, populate_existing=lock)
    if row is None or row.organization_id != principal.organization_id:
        raise NotFoundError("Rule set not found")
    return row


async def active(db: AsyncSession, product_id: uuid.UUID) -> RuleSet | None:
    return await db.scalar(
        select(RuleSet).where(RuleSet.product_id == product_id, RuleSet.status == "published")
    )


async def create_draft(
    db: AsyncSession,
    principal: Principal,
    product_id: uuid.UUID,
    actor: AuditActor,
    config: dict[str, Any] | None,
    note: str,
) -> RuleSet:
    product = await _product(db, principal, product_id)
    if await db.scalar(select(RuleSet.id).where(RuleSet.product_id == product.id, RuleSet.status == "draft")):
        raise ConflictError("This product already has a draft rule set; edit or publish it first")
    current = await active(db, product.id)
    base = (
        config
        if config is not None
        else (current.config if current else DEFAULT_CONFIG.model_dump(mode="json"))
    )
    clean = validate(base)
    latest = await db.scalar(select(func.max(RuleSet.version_no)).where(RuleSet.product_id == product.id))
    row = RuleSet(
        id=uuid.uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version_no=(latest or 0) + 1,
        status="draft",
        config=clean,
        config_sha256=_sha(clean),
        change_note=note[:500],
        created_by=principal.user_id,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="rule_set.draft_created",
        entity_type="product",
        entity_id=product.id,
        after={"rule_set_id": str(row.id), "version": row.version_no},
    )
    await db.commit()
    return row


async def update_draft(
    db: AsyncSession,
    principal: Principal,
    rule_set_id: uuid.UUID,
    actor: AuditActor,
    config: dict[str, Any],
    note: str | None,
    expected_version: int,
) -> RuleSet:
    row = await get(db, principal, rule_set_id, lock=True)
    if row.status != "draft":
        raise ConflictError(f"Version {row.version_no} is {row.status}; create a new draft to change it")
    if row.row_version != expected_version:
        raise ConflictError("The draft was changed by someone else; reload it")
    clean = validate(config)
    row.config, row.config_sha256 = clean, _sha(clean)
    if note is not None:
        row.change_note = note[:500]
    row.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="rule_set.draft_updated",
        entity_type="product",
        entity_id=row.product_id,
        after={"rule_set_id": str(row.id), "sha256": row.config_sha256},
    )
    await db.commit()
    return row


async def publish(
    db: AsyncSession, principal: Principal, rule_set_id: uuid.UUID, actor: AuditActor, expected_version: int
) -> RuleSet:
    row = await get(db, principal, rule_set_id, lock=True)
    if row.status != "draft":
        raise ConflictError(f"Version {row.version_no} is already {row.status}")
    if row.row_version != expected_version:
        raise ConflictError("The draft was changed by someone else; reload it")
    validate(row.config)  # re-validated at publish time
    previous = await active(db, row.product_id)
    if previous is not None:
        previous.status = "retired"
        await db.flush()
    row.status, row.published_by, row.published_at = "published", principal.user_id, datetime.now(UTC)
    row.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="rule_set.published",
        entity_type="product",
        entity_id=row.product_id,
        before={"previous": str(previous.id) if previous else None},
        after={"rule_set_id": str(row.id), "version": row.version_no, "sha256": row.config_sha256},
    )
    await db.commit()
    return row
