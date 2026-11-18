"""Products, customers, policies and policy import."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.deps import ActorDep, DbDep, PrincipalDep, etag, parse_if_match, require
from app.models.catalog import Customer, InsuranceProduct, Policy
from app.schemas.catalog import (
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
    PolicyCreate,
    PolicyImportReport,
    PolicyImportRequest,
    PolicyOut,
    PolicySummary,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    PublishRequest,
    RequirementIn,
    VersionIn,
    VersionOut,
)
from app.schemas.common import Page
from app.security.permissions import P
from app.security.principal import Principal
from app.services import policy_import, policy_service, product_service
from app.services.catalog_mapping import product_out, version_out, version_spec
from app.services.product_service import RequirementSpec

router = APIRouter(tags=["catalog"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
ProductAdmin = Annotated[Principal, Depends(require(P.PRODUCTS_MANAGE))]
PolicyAdmin = Annotated[Principal, Depends(require(P.POLICIES_MANAGE))]
IfMatch = Annotated[str | None, Header()]


def _summary(policy: Policy, customer: Customer, product: InsuranceProduct) -> PolicySummary:
    return PolicySummary(
        id=policy.id,
        policy_number=policy.policy_number,
        status=policy.status,
        customer_id=customer.id,
        customer_name=customer.full_name,
        product_id=product.id,
        product_name=product.name,
        product_line=product.line,
        currency=product.currency,
        is_synthetic=policy.is_synthetic,
    )


# ---- Products -----------------------------------------------------------------------------


@router.get("/products", response_model=list[ProductOut])
async def list_products(principal: PrincipalDep, db: DbDep) -> list[ProductOut]:
    return [product_out(v) for v in await product_service.list_products(db, principal)]


@router.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductCreate, principal: ProductAdmin, actor: ActorDep, db: DbDep
) -> ProductOut:
    reqs = (
        [RequirementSpec(r.document_type, r.mandatory, r.description) for r in body.document_requirements]
        if body.document_requirements is not None
        else None
    )
    view = await product_service.create_product(
        db,
        principal,
        actor,
        code=body.code,
        name=body.name,
        line=body.line,
        currency=body.currency,
        description=body.description,
        requirements=reqs,
    )
    return product_out(view)


@router.get("/products/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: uuid.UUID, principal: PrincipalDep, db: DbDep, response: Response
) -> ProductOut:
    view = await product_service.get_product(db, principal, product_id)
    response.headers["ETag"] = etag(view.product.row_version)
    return product_out(view)


@router.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    principal: ProductAdmin,
    actor: ActorDep,
    db: DbDep,
    if_match: IfMatch = None,
) -> ProductOut:
    view = await product_service.update_product(
        db,
        principal,
        actor,
        product_id,
        expected_version=parse_if_match(if_match),
        changes=body.model_dump(exclude_unset=True),
    )
    return product_out(view)


@router.put("/products/{product_id}/document-requirements", response_model=ProductOut)
async def set_requirements(
    product_id: uuid.UUID, body: list[RequirementIn], principal: ProductAdmin, actor: ActorDep, db: DbDep
) -> ProductOut:
    specs = [RequirementSpec(r.document_type, r.mandatory, r.description) for r in body]
    return product_out(await product_service.set_requirements(db, principal, actor, product_id, specs))


# ---- Customers ----------------------------------------------------------------------------


@router.get("/customers", response_model=Page[CustomerOut])
async def list_customers(
    principal: Reader,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[CustomerOut]:
    rows, total = await product_service.list_customers(db, principal, q=q, page=page, page_size=page_size)
    return Page(
        items=[CustomerOut.model_validate(c) for c in rows], page=page, page_size=page_size, total=total
    )


@router.post("/customers", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
async def create_customer(
    body: CustomerCreate, principal: PolicyAdmin, actor: ActorDep, db: DbDep
) -> CustomerOut:
    values = body.model_dump()
    return CustomerOut.model_validate(await product_service.create_customer(db, principal, actor, values))


@router.get("/customers/{customer_id}", response_model=CustomerOut)
async def get_customer(
    customer_id: uuid.UUID, principal: Reader, db: DbDep, response: Response
) -> CustomerOut:
    customer = await product_service.get_customer(db, principal, customer_id)
    response.headers["ETag"] = etag(customer.row_version)
    return CustomerOut.model_validate(customer)


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: uuid.UUID,
    body: CustomerUpdate,
    principal: PolicyAdmin,
    actor: ActorDep,
    db: DbDep,
    if_match: IfMatch = None,
) -> CustomerOut:
    customer = await product_service.update_customer(
        db,
        principal,
        actor,
        customer_id,
        expected_version=parse_if_match(if_match),
        changes=body.model_dump(exclude_unset=True),
    )
    return CustomerOut.model_validate(customer)


# ---- Policies -----------------------------------------------------------------------------


@router.get("/policies", response_model=Page[PolicySummary])
async def list_policies(
    principal: Reader,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    customer_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[PolicySummary]:
    rows, total = await policy_service.list_policies(
        db, principal, q=q, customer_id=customer_id, page=page, page_size=page_size
    )
    return Page(items=[_summary(*r) for r in rows], page=page, page_size=page_size, total=total)


async def _policy_out(db: DbDep, principal: Principal, policy_id: uuid.UUID) -> PolicyOut:
    view = await policy_service.get_policy(db, principal, policy_id)
    base = _summary(view.policy, view.customer, view.product)
    return PolicyOut(
        **base.model_dump(),
        source=view.policy.source,
        external_ref=view.policy.external_ref,
        versions=[version_out(v) for v in view.versions],
    )


@router.post("/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(body: PolicyCreate, principal: PolicyAdmin, actor: ActorDep, db: DbDep) -> PolicyOut:
    policy = await policy_service.create_policy(
        db,
        principal,
        actor,
        policy_number=body.policy_number,
        customer_id=body.customer_id,
        product_id=body.product_id,
        status=body.status,
        external_ref=body.external_ref,
        is_synthetic=body.is_synthetic,
    )
    if body.initial_version is not None:
        view = await policy_service.add_version(
            db, principal, actor, policy.id, version_spec(body.initial_version), commit=False
        )
        if body.publish_initial_version:
            await policy_service.publish_version(
                db, principal, actor, policy.id, view.version.id, commit=False
            )
    await db.commit()
    return await _policy_out(db, principal, policy.id)


@router.post("/policies/import", response_model=PolicyImportReport)
async def import_policies(
    body: PolicyImportRequest, principal: PolicyAdmin, actor: ActorDep, db: DbDep
) -> PolicyImportReport:
    """All-or-nothing import. ``dry_run`` (default true) validates everything and saves nothing."""
    report = await policy_import.import_policies(db, principal, actor, body)
    return PolicyImportReport(
        dry_run=report.dry_run,
        policies_created=report.policies_created,
        customers_created=report.customers_created,
        versions_created=report.versions_created,
        versions_published=report.versions_published,
        policy_numbers=report.policy_numbers,
    )


@router.get("/policies/{policy_id}", response_model=PolicyOut)
async def get_policy(policy_id: uuid.UUID, principal: Reader, db: DbDep) -> PolicyOut:
    return await _policy_out(db, principal, policy_id)


@router.post("/policies/{policy_id}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED)
async def add_version(
    policy_id: uuid.UUID, body: VersionIn, principal: PolicyAdmin, actor: ActorDep, db: DbDep
) -> VersionOut:
    return version_out(await policy_service.add_version(db, principal, actor, policy_id, version_spec(body)))


@router.post("/policies/{policy_id}/versions/{version_id}/publish", response_model=VersionOut)
async def publish_version(
    policy_id: uuid.UUID,
    version_id: uuid.UUID,
    principal: PolicyAdmin,
    actor: ActorDep,
    db: DbDep,
    body: PublishRequest | None = None,
) -> VersionOut:
    """Publish a draft (immutable afterwards). List versions it replaces in ``supersede_version_ids``."""
    supersede = body.supersede_version_ids if body else []
    return version_out(
        await policy_service.publish_version(db, principal, actor, policy_id, version_id, supersede=supersede)
    )
