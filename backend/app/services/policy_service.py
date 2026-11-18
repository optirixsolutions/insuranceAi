"""Policies and their versioned terms (ADR-003: structured policy versions are the source of truth).

A version is drafted with its coverages, exclusions and clauses, then published. Published
terms are immutable (enforced by database triggers) and published periods of one policy can
never overlap (exclusion constraint). A mid-term change publishes replacement versions and
supersedes the old one in the same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models.catalog import (
    Customer,
    InsuranceProduct,
    Policy,
    PolicyClause,
    PolicyCoverage,
    PolicyExclusion,
    PolicyVersion,
)
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from app.services.common import ilike_any
from rules_engine.money import MoneyError, validate_amount


@dataclass(frozen=True, slots=True)
class ClauseSpec:
    clause_ref: str
    section: str
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class CoverageSpec:
    coverage_code: str
    perils: list[str]
    limit_amount: Decimal
    clause_ref: str
    deductible_override: Decimal | None = None
    co_pay_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExclusionSpec:
    exclusion_code: str
    applies_to: list[str]
    description: str
    clause_ref: str


@dataclass(frozen=True, slots=True)
class VersionSpec:
    effective_from: date
    effective_to: date
    sum_insured: Decimal
    deductible_amount: Decimal
    annual_aggregate_limit: Decimal | None = None
    waiting_period_days: int = 0
    endorsements: list[dict[str, Any]] = field(default_factory=list)
    clauses: list[ClauseSpec] = field(default_factory=list)
    coverages: list[CoverageSpec] = field(default_factory=list)
    exclusions: list[ExclusionSpec] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class VersionView:
    version: PolicyVersion
    clauses: list[PolicyClause]
    coverages: list[PolicyCoverage]
    exclusions: list[PolicyExclusion]


@dataclass(frozen=True, slots=True)
class PolicyView:
    policy: Policy
    customer: Customer
    product: InsuranceProduct
    versions: list[VersionView]


async def _policy(db: AsyncSession, principal: Principal, policy_id: uuid.UUID) -> Policy:
    policy = await db.scalar(
        select(Policy).where(
            Policy.organization_id == principal.organization_id,
            Policy.id == policy_id,
            Policy.deleted_at.is_(None),
        )
    )
    if policy is None:
        raise NotFoundError("Policy not found")
    return policy


async def _version_view(db: AsyncSession, version: PolicyVersion) -> VersionView:
    vid = version.id
    clauses = (
        await db.scalars(
            select(PolicyClause)
            .where(PolicyClause.policy_version_id == vid)
            .order_by(PolicyClause.clause_ref)
        )
    ).all()
    coverages = (
        await db.scalars(
            select(PolicyCoverage)
            .where(PolicyCoverage.policy_version_id == vid)
            .order_by(PolicyCoverage.coverage_code)
        )
    ).all()
    exclusions = (
        await db.scalars(
            select(PolicyExclusion)
            .where(PolicyExclusion.policy_version_id == vid)
            .order_by(PolicyExclusion.exclusion_code)
        )
    ).all()
    return VersionView(version, list(clauses), list(coverages), list(exclusions))


async def get_policy(db: AsyncSession, principal: Principal, policy_id: uuid.UUID) -> PolicyView:
    policy = await _policy(db, principal, policy_id)
    customer = await db.get(Customer, policy.customer_id)
    product = await db.get(InsuranceProduct, policy.product_id)
    assert customer is not None and product is not None  # composite FKs guarantee both exist
    versions = (
        await db.scalars(
            select(PolicyVersion)
            .where(PolicyVersion.policy_id == policy.id)
            .order_by(PolicyVersion.version_no)
        )
    ).all()
    return PolicyView(policy, customer, product, [await _version_view(db, v) for v in versions])


async def list_policies(
    db: AsyncSession,
    principal: Principal,
    *,
    q: str | None,
    customer_id: uuid.UUID | None,
    page: int,
    page_size: int,
) -> tuple[list[tuple[Policy, Customer, InsuranceProduct]], int]:
    base = (
        select(Policy, Customer, InsuranceProduct)
        .join(Customer, Customer.id == Policy.customer_id)
        .join(InsuranceProduct, InsuranceProduct.id == Policy.product_id)
        .where(Policy.organization_id == principal.organization_id, Policy.deleted_at.is_(None))
    )
    if q:
        base = base.where(ilike_any(q, Policy.policy_number, Customer.full_name))
    if customer_id:
        base = base.where(Policy.customer_id == customer_id)
    total = int(await db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (
        await db.execute(base.order_by(Policy.policy_number).offset((page - 1) * page_size).limit(page_size))
    ).all()
    return [(r[0], r[1], r[2]) for r in rows], total


async def create_policy(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    *,
    policy_number: str,
    customer_id: uuid.UUID,
    product_id: uuid.UUID,
    status: str = "active",
    source: str = "manual",
    external_ref: str | None = None,
    is_synthetic: bool = False,
) -> Policy:
    org = principal.organization_id
    if (
        await db.scalar(
            select(Customer.id).where(
                Customer.organization_id == org, Customer.id == customer_id, Customer.deleted_at.is_(None)
            )
        )
        is None
    ):
        raise NotFoundError("Customer not found")
    if (
        await db.scalar(
            select(InsuranceProduct.id).where(
                InsuranceProduct.organization_id == org, InsuranceProduct.id == product_id
            )
        )
        is None
    ):
        raise NotFoundError("Product not found")
    policy = Policy(
        organization_id=org,
        policy_number=policy_number,
        customer_id=customer_id,
        product_id=product_id,
        status=status,
        source=source,
        external_ref=external_ref,
        is_synthetic=is_synthetic,
    )
    db.add(policy)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(f"Policy number '{policy_number}' already exists") from exc
    await audit_service.record(
        db,
        actor=actor,
        action="policy.created",
        entity_type="policy",
        entity_id=policy.id,
        after={"policy_number": policy_number, "product_id": str(product_id)},
    )
    return policy


def _validate_spec(spec: VersionSpec, currency: str) -> None:
    if spec.effective_to <= spec.effective_from:
        raise BusinessRuleError("effective_to must be after effective_from")
    try:
        validate_amount(spec.sum_insured, currency)
        validate_amount(spec.deductible_amount, currency, allow_zero=True)
        if spec.annual_aggregate_limit is not None:
            validate_amount(spec.annual_aggregate_limit, currency)
        for cov in spec.coverages:
            validate_amount(cov.limit_amount, currency)
            if cov.deductible_override is not None:
                validate_amount(cov.deductible_override, currency, allow_zero=True)
    except MoneyError as exc:
        raise BusinessRuleError(str(exc)) from exc
    refs = {c.clause_ref for c in spec.clauses}
    if len(refs) != len(spec.clauses):
        raise BusinessRuleError("Clause references must be unique within a version")
    cited = {c.clause_ref for c in spec.coverages} | {e.clause_ref for e in spec.exclusions}
    missing = sorted(cited - refs)
    if missing:
        raise BusinessRuleError(f"Coverages/exclusions cite unknown clauses: {', '.join(missing)}")


async def add_version(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    policy_id: uuid.UUID,
    spec: VersionSpec,
    *,
    commit: bool = True,
) -> VersionView:
    policy = await _policy(db, principal, policy_id)
    product = await db.get(InsuranceProduct, policy.product_id)
    assert product is not None
    _validate_spec(spec, product.currency)
    next_no = (
        int(
            await db.scalar(
                select(func.coalesce(func.max(PolicyVersion.version_no), 0)).where(
                    PolicyVersion.policy_id == policy.id
                )
            )
            or 0
        )
        + 1
    )
    org = principal.organization_id
    version = PolicyVersion(
        organization_id=org,
        policy_id=policy.id,
        version_no=next_no,
        effective_from=spec.effective_from,
        effective_to=spec.effective_to,
        currency=product.currency,
        sum_insured=spec.sum_insured,
        deductible_amount=spec.deductible_amount,
        annual_aggregate_limit=spec.annual_aggregate_limit,
        waiting_period_days=spec.waiting_period_days,
        endorsements=spec.endorsements,
        status="draft",
    )
    db.add(version)
    await db.flush()
    for c in spec.clauses:
        db.add(
            PolicyClause(
                organization_id=org,
                policy_version_id=version.id,
                clause_ref=c.clause_ref,
                section=c.section,
                title=c.title,
                text=c.text,
            )
        )
    for cov in spec.coverages:
        db.add(
            PolicyCoverage(
                organization_id=org,
                policy_version_id=version.id,
                coverage_code=cov.coverage_code,
                perils=cov.perils,
                limit_amount=cov.limit_amount,
                clause_ref=cov.clause_ref,
                deductible_override=cov.deductible_override,
                co_pay_pct=cov.co_pay_pct,
            )
        )
    for ex in spec.exclusions:
        db.add(
            PolicyExclusion(
                organization_id=org,
                policy_version_id=version.id,
                exclusion_code=ex.exclusion_code,
                applies_to=ex.applies_to,
                description=ex.description,
                clause_ref=ex.clause_ref,
            )
        )
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="policy.version_drafted",
        entity_type="policy_version",
        entity_id=version.id,
        after={
            "policy_id": str(policy.id),
            "version_no": next_no,
            "effective_from": str(spec.effective_from),
            "effective_to": str(spec.effective_to),
        },
    )
    if commit:
        await db.commit()
    return await _version_view(db, version)


async def publish_version(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    policy_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    supersede: list[uuid.UUID] | None = None,
    commit: bool = True,
) -> VersionView:
    policy = await _policy(db, principal, policy_id)
    version = await db.scalar(
        select(PolicyVersion).where(PolicyVersion.id == version_id, PolicyVersion.policy_id == policy.id)
    )
    if version is None:
        raise NotFoundError("Policy version not found")
    if version.status != "draft":
        raise BusinessRuleError("Only draft versions can be published")
    view = await _version_view(db, version)
    if not view.coverages:
        raise BusinessRuleError("A version needs at least one coverage before it can be published")
    for old_id in supersede or []:
        old = await db.scalar(
            select(PolicyVersion).where(PolicyVersion.id == old_id, PolicyVersion.policy_id == policy.id)
        )
        if old is None or old.status != "published":
            raise BusinessRuleError("Only published versions of this policy can be superseded")
        old.status = "superseded"
    await db.flush()
    version.status, version.published_at, version.published_by = (
        "published",
        datetime.now(UTC),
        principal.user_id,
    )
    try:
        await db.flush()
    except (IntegrityError, DBAPIError) as exc:
        await db.rollback()
        if "ex_policy_versions_no_overlap" in str(exc):
            raise ConflictError(
                "This period overlaps another published version of the policy. "
                "Supersede that version or adjust the dates."
            ) from exc
        raise
    await audit_service.record(
        db,
        actor=actor,
        action="policy.version_published",
        entity_type="policy_version",
        entity_id=version.id,
        after={
            "policy_id": str(policy.id),
            "version_no": version.version_no,
            "superseded": [str(s) for s in supersede or []],
        },
    )
    if commit:
        await db.commit()
    return await _version_view(db, version)


async def version_on(db: AsyncSession, policy_id: uuid.UUID, on: date) -> PolicyVersion | None:
    """The published version in force on ``on`` (effective_from inclusive, effective_to exclusive)."""
    return await db.scalar(
        select(PolicyVersion).where(
            PolicyVersion.policy_id == policy_id,
            PolicyVersion.status == "published",
            PolicyVersion.effective_from <= on,
            PolicyVersion.effective_to > on,
        )
    )
