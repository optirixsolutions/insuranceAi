"""Bulk import of customers, policies and versioned terms from JSON.

The whole import is one transaction: either every policy is created or none is.
``dry_run`` performs every check (including database constraints) and then rolls back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BusinessRuleError
from app.models.catalog import Customer, InsuranceProduct
from app.schemas.catalog import PolicyImportItem, PolicyImportRequest
from app.security.principal import Principal
from app.services import audit_service, policy_service
from app.services.audit_service import AuditActor
from app.services.catalog_mapping import version_spec


@dataclass(slots=True)
class ImportReport:
    dry_run: bool
    policies_created: int = 0
    customers_created: int = 0
    versions_created: int = 0
    versions_published: int = 0
    policy_numbers: list[str] = field(default_factory=list)


async def _customer(
    db: AsyncSession, principal: Principal, item: PolicyImportItem, report: ImportReport
) -> Customer:
    spec = item.customer
    existing = await db.scalar(
        select(Customer).where(
            Customer.organization_id == principal.organization_id,
            Customer.external_ref == spec.external_ref,
            Customer.deleted_at.is_(None),
        )
    )
    if existing is not None:
        return existing
    customer = Customer(organization_id=principal.organization_id, **spec.model_dump())
    db.add(customer)
    await db.flush()
    report.customers_created += 1
    return customer


async def import_policies(
    db: AsyncSession, principal: Principal, actor: AuditActor, request: PolicyImportRequest
) -> ImportReport:
    report = ImportReport(dry_run=request.dry_run)
    products = {
        p.code: p
        for p in (
            await db.scalars(
                select(InsuranceProduct).where(InsuranceProduct.organization_id == principal.organization_id)
            )
        ).all()
    }
    try:
        for index, item in enumerate(request.policies):
            where = f"policies[{index}] ({item.policy_number})"
            product = products.get(item.product_code)
            if product is None:
                raise BusinessRuleError(f"{where}: unknown product code '{item.product_code}'")
            customer = await _customer(db, principal, item, report)
            policy = await policy_service.create_policy(
                db,
                principal,
                actor,
                policy_number=item.policy_number,
                customer_id=customer.id,
                product_id=product.id,
                status=item.status,
                source="import",
                external_ref=item.external_ref,
                is_synthetic=item.is_synthetic,
            )
            for version in item.versions:
                view = await policy_service.add_version(
                    db, principal, actor, policy.id, version_spec(version), commit=False
                )
                report.versions_created += 1
                if version.publish:
                    await policy_service.publish_version(
                        db, principal, actor, policy.id, view.version.id, commit=False
                    )
                    report.versions_published += 1
            report.policies_created += 1
            report.policy_numbers.append(item.policy_number)
    except AppError as exc:
        await db.rollback()
        detail = exc.detail or exc.title
        raise BusinessRuleError(f"Import rejected, nothing was saved: {detail}") from exc

    if request.dry_run:
        await db.rollback()
        return report
    await audit_service.record(
        db,
        actor=actor,
        action="policy.imported",
        entity_type="policy",
        entity_id=None,
        after={"policies": report.policies_created, "versions": report.versions_created},
    )
    await db.commit()
    return report
