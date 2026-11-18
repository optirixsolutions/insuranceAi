"""Insurance products (with document requirements) and customers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models.catalog import Customer, DocumentRequirement, InsuranceProduct
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from app.services.common import changed_fields, check_version, ilike_any, jsonable
from rules_engine.intake import DEFAULT_DOCUMENTS
from rules_engine.money import MoneyError, currency_exponent


@dataclass(frozen=True, slots=True)
class RequirementSpec:
    document_type: str
    mandatory: bool
    description: str = ""


@dataclass(frozen=True, slots=True)
class ProductView:
    product: InsuranceProduct
    requirements: list[DocumentRequirement]


async def _product(db: AsyncSession, principal: Principal, product_id: uuid.UUID) -> InsuranceProduct:
    product = await db.scalar(
        select(InsuranceProduct).where(
            InsuranceProduct.organization_id == principal.organization_id, InsuranceProduct.id == product_id
        )
    )
    if product is None:
        raise NotFoundError("Product not found")
    return product


async def _requirements(db: AsyncSession, product_id: uuid.UUID) -> list[DocumentRequirement]:
    rows = await db.scalars(
        select(DocumentRequirement)
        .where(DocumentRequirement.product_id == product_id)
        .order_by(DocumentRequirement.mandatory.desc(), DocumentRequirement.document_type)
    )
    return list(rows.all())


async def list_products(db: AsyncSession, principal: Principal) -> list[ProductView]:
    products = (
        await db.scalars(
            select(InsuranceProduct)
            .where(InsuranceProduct.organization_id == principal.organization_id)
            .order_by(InsuranceProduct.name)
        )
    ).all()
    return [ProductView(p, await _requirements(db, p.id)) for p in products]


async def get_product(db: AsyncSession, principal: Principal, product_id: uuid.UUID) -> ProductView:
    product = await _product(db, principal, product_id)
    return ProductView(product, await _requirements(db, product.id))


async def create_product(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    *,
    code: str,
    name: str,
    line: str,
    currency: str,
    description: str,
    requirements: list[RequirementSpec] | None,
) -> ProductView:
    try:
        currency_exponent(currency)
    except MoneyError as exc:
        raise BusinessRuleError(str(exc)) from exc
    product = InsuranceProduct(
        organization_id=principal.organization_id,
        code=code,
        name=name,
        line=line,
        currency=currency,
        description=description,
    )
    db.add(product)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(f"A product with code '{code}' already exists") from exc
    specs = (
        requirements
        if requirements is not None
        else [RequirementSpec(doc, mandatory) for doc, mandatory in DEFAULT_DOCUMENTS[line]]
    )
    for spec in specs:
        db.add(
            DocumentRequirement(
                organization_id=principal.organization_id,
                product_id=product.id,
                document_type=spec.document_type,
                mandatory=spec.mandatory,
                description=spec.description,
            )
        )
    await audit_service.record(
        db,
        actor=actor,
        action="product.created",
        entity_type="insurance_product",
        entity_id=product.id,
        after={"code": code, "name": name, "line": line, "currency": currency},
    )
    await db.commit()
    return await get_product(db, principal, product.id)


async def update_product(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    product_id: uuid.UUID,
    *,
    expected_version: int,
    changes: dict[str, Any],
) -> ProductView:
    product = await _product(db, principal, product_id)
    check_version(product.row_version, expected_version, "product")
    before, after = changed_fields(product, changes)
    if after:
        product.row_version += 1
        await audit_service.record(
            db,
            actor=actor,
            action="product.updated",
            entity_type="insurance_product",
            entity_id=product.id,
            before=jsonable(before),
            after=jsonable(after),
        )
    await db.commit()
    return await get_product(db, principal, product.id)


async def set_requirements(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    product_id: uuid.UUID,
    specs: list[RequirementSpec],
) -> ProductView:
    product = await _product(db, principal, product_id)
    if len({s.document_type for s in specs}) != len(specs):
        raise BusinessRuleError("Each document type may appear only once")
    before = [r.document_type + ("*" if r.mandatory else "") for r in await _requirements(db, product.id)]
    await db.execute(delete(DocumentRequirement).where(DocumentRequirement.product_id == product.id))
    for spec in specs:
        db.add(
            DocumentRequirement(
                organization_id=principal.organization_id,
                product_id=product.id,
                document_type=spec.document_type,
                mandatory=spec.mandatory,
                description=spec.description,
            )
        )
    await audit_service.record(
        db,
        actor=actor,
        action="product.requirements_changed",
        entity_type="insurance_product",
        entity_id=product.id,
        before={"requirements": before},
        after={"requirements": [s.document_type + ("*" if s.mandatory else "") for s in specs]},
    )
    await db.commit()
    return await get_product(db, principal, product.id)


# ---- Customers ---------------------------------------------------------------------------------


async def get_customer(db: AsyncSession, principal: Principal, customer_id: uuid.UUID) -> Customer:
    customer = await db.scalar(
        select(Customer).where(
            Customer.organization_id == principal.organization_id,
            Customer.id == customer_id,
            Customer.deleted_at.is_(None),
        )
    )
    if customer is None:
        raise NotFoundError("Customer not found")
    return customer


async def list_customers(
    db: AsyncSession, principal: Principal, *, q: str | None, page: int, page_size: int
) -> tuple[list[Customer], int]:
    conditions = [Customer.organization_id == principal.organization_id, Customer.deleted_at.is_(None)]
    if q:
        conditions.append(ilike_any(q, Customer.full_name, Customer.email, Customer.external_ref))
    total = int(await db.scalar(select(func.count()).select_from(Customer).where(*conditions)) or 0)
    rows = await db.scalars(
        select(Customer)
        .where(*conditions)
        .order_by(Customer.full_name, Customer.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total


async def create_customer(
    db: AsyncSession, principal: Principal, actor: AuditActor, values: dict[str, Any]
) -> Customer:
    customer = Customer(organization_id=principal.organization_id, **values)
    db.add(customer)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("A customer with this external reference already exists") from exc
    # Personal data is not copied into the audit log; the entity id identifies the record.
    await audit_service.record(
        db,
        actor=actor,
        action="customer.created",
        entity_type="customer",
        entity_id=customer.id,
        after={"is_synthetic": customer.is_synthetic},
    )
    await db.commit()
    return customer


async def update_customer(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    customer_id: uuid.UUID,
    *,
    expected_version: int,
    changes: dict[str, Any],
) -> Customer:
    customer = await get_customer(db, principal, customer_id)
    check_version(customer.row_version, expected_version, "customer")
    _, after = changed_fields(customer, changes)
    if after:
        customer.row_version += 1
        await audit_service.record(
            db,
            actor=actor,
            action="customer.updated",
            entity_type="customer",
            entity_id=customer.id,
            after={"fields_changed": sorted(after)},
        )
    customer.updated_at = datetime.now(UTC)
    await db.commit()
    return await get_customer(db, principal, customer.id)
