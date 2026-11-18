"""Convert validated API schemas into service specs and service views into API output."""

from __future__ import annotations

from decimal import Decimal

from app.schemas.catalog import ProductOut, RequirementOut, VersionIn, VersionOut
from app.services.policy_service import ClauseSpec, CoverageSpec, ExclusionSpec, VersionSpec, VersionView
from app.services.product_service import ProductView
from rules_engine.money import to_minor_units


def version_spec(v: VersionIn) -> VersionSpec:
    return VersionSpec(
        effective_from=v.effective_from,
        effective_to=v.effective_to,
        sum_insured=v.sum_insured,
        deductible_amount=v.deductible_amount,
        annual_aggregate_limit=v.annual_aggregate_limit,
        waiting_period_days=v.waiting_period_days,
        endorsements=v.endorsements,
        clauses=[ClauseSpec(c.clause_ref, c.section, c.title, c.text) for c in v.clauses],
        coverages=[
            CoverageSpec(
                c.coverage_code, c.perils, c.limit_amount, c.clause_ref, c.deductible_override, c.co_pay_pct
            )
            for c in v.coverages
        ],
        exclusions=[
            ExclusionSpec(e.exclusion_code, e.applies_to, e.description, e.clause_ref) for e in v.exclusions
        ],
    )


def version_out(view: VersionView) -> VersionOut:
    v = view.version

    def m(amount: Decimal | None) -> Decimal | None:
        return None if amount is None else to_minor_units(amount, v.currency)

    out = VersionOut.model_validate(
        {
            "id": v.id,
            "version_no": v.version_no,
            "status": v.status,
            "effective_from": v.effective_from,
            "effective_to": v.effective_to,
            "currency": v.currency,
            "sum_insured": v.sum_insured,
            "deductible_amount": v.deductible_amount,
            "annual_aggregate_limit": v.annual_aggregate_limit,
            "waiting_period_days": v.waiting_period_days,
            "endorsements": v.endorsements,
            "published_at": v.published_at,
            "clauses": view.clauses,
            "coverages": view.coverages,
            "exclusions": view.exclusions,
        },
        from_attributes=True,
    )
    out.sum_insured = to_minor_units(v.sum_insured, v.currency)
    out.deductible_amount = to_minor_units(v.deductible_amount, v.currency)
    out.annual_aggregate_limit = m(v.annual_aggregate_limit)
    for cov in out.coverages:
        cov.limit_amount = to_minor_units(cov.limit_amount, v.currency)
        cov.deductible_override = m(cov.deductible_override)
    return out


def product_out(view: ProductView) -> ProductOut:
    p = view.product
    return ProductOut(
        id=p.id,
        code=p.code,
        name=p.name,
        line=p.line,
        currency=p.currency,
        description=p.description,
        status=p.status,
        row_version=p.row_version,
        document_requirements=[RequirementOut.model_validate(r) for r in view.requirements],
    )
