"""Load the read-only ``ClaimSnapshot`` an agent run works on (one organisation, one claim, RLS)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.snapshot import (
    ClaimRef,
    ClaimSnap,
    ClaimSnapshot,
    ClauseSnap,
    CoverageSnap,
    CustomerSnap,
    DocumentSnap,
    ExclusionSnap,
    FieldSnap,
    InvoiceRef,
    PageSnap,
    PolicySnap,
    PriorOutput,
    RequiredDoc,
    VersionSnap,
)
from app.models.agents import AgentOutput, AgentRun
from app.models.catalog import (
    Customer,
    DocumentRequirement,
    InsuranceProduct,
    Policy,
    PolicyClause,
    PolicyCoverage,
    PolicyExclusion,
    PolicyVersion,
)
from app.models.claims import Claim
from app.models.documents import ClaimDocument, DocumentPage, ExtractedField
from app.services import ai_service, rule_set_service

BASELINE_LIMIT = 2000


def _ref(c: Claim) -> ClaimRef:
    return ClaimRef(
        id=str(c.id),
        claim_reference=c.claim_reference,
        policy_id=str(c.policy_id),
        incident_date=c.incident_date,
        claimed_amount=c.claimed_amount,
        currency=c.currency,
        status=c.status,
        created_at=c.created_at,
    )


async def _versions(db: AsyncSession, policy_id: uuid.UUID) -> list[VersionSnap]:
    versions = (await db.scalars(select(PolicyVersion).where(PolicyVersion.policy_id == policy_id))).all()
    ids = [v.id for v in versions]
    if not ids:
        return []
    clauses = (await db.scalars(select(PolicyClause).where(PolicyClause.policy_version_id.in_(ids)))).all()
    coverages = (
        await db.scalars(select(PolicyCoverage).where(PolicyCoverage.policy_version_id.in_(ids)))
    ).all()
    exclusions = (
        await db.scalars(select(PolicyExclusion).where(PolicyExclusion.policy_version_id.in_(ids)))
    ).all()
    return [
        VersionSnap(
            id=str(v.id),
            version_no=v.version_no,
            status=v.status,
            effective_from=v.effective_from,
            effective_to=v.effective_to,
            currency=v.currency,
            sum_insured=v.sum_insured,
            deductible_amount=v.deductible_amount,
            annual_aggregate_limit=v.annual_aggregate_limit,
            waiting_period_days=v.waiting_period_days,
            endorsements=list(v.endorsements or []),
            coverages=[
                CoverageSnap(
                    coverage_code=c.coverage_code,
                    perils=list(c.perils),
                    limit_amount=c.limit_amount,
                    deductible_override=c.deductible_override,
                    co_pay_pct=c.co_pay_pct,
                    clause_ref=c.clause_ref,
                )
                for c in coverages
                if c.policy_version_id == v.id
            ],
            exclusions=[
                ExclusionSnap(
                    exclusion_code=e.exclusion_code,
                    applies_to=list(e.applies_to),
                    description=e.description,
                    clause_ref=e.clause_ref,
                )
                for e in exclusions
                if e.policy_version_id == v.id
            ],
            clauses=[
                ClauseSnap(
                    id=str(c.id), clause_ref=c.clause_ref, section=c.section, title=c.title, text=c.text
                )
                for c in clauses
                if c.policy_version_id == v.id
            ],
        )
        for v in versions
    ]


async def _documents(db: AsyncSession, claim_id: uuid.UUID) -> list[DocumentSnap]:
    docs = (
        await db.scalars(
            select(ClaimDocument)
            .where(ClaimDocument.claim_id == claim_id, ClaimDocument.deleted_at.is_(None))
            .order_by(ClaimDocument.created_at)
        )
    ).all()
    superseded = {
        d.supersedes_id for d in docs if d.supersedes_id
    }  # a re-upload replaces the earlier version
    docs = [d for d in docs if d.id not in superseded]
    ids = [d.id for d in docs]
    if not ids:
        return []
    pages = (
        await db.scalars(
            select(DocumentPage).where(DocumentPage.document_id.in_(ids)).order_by(DocumentPage.page_number)
        )
    ).all()
    fields = (
        await db.scalars(
            select(ExtractedField).where(
                ExtractedField.document_id.in_(ids), ExtractedField.is_current.is_(True)
            )
        )
    ).all()
    return [
        DocumentSnap(
            id=str(d.id),
            filename=d.original_filename,
            document_type=d.document_type,
            classification_confidence=d.classification_confidence,
            classification_source=d.classification_source,
            scan_status=d.scan_status,
            processing_status=d.processing_status,
            quality_score=d.quality_score,
            review_reasons=list(d.review_reasons or []),
            duplicates=list(d.duplicates or []),
            pages=[
                PageSnap(
                    page_number=p.page_number,
                    text=p.text,
                    ocr_confidence=p.ocr_confidence,
                    quality_score=p.quality_score,
                )
                for p in pages
                if p.document_id == d.id
            ],
            fields=[
                FieldSnap(
                    id=str(f.id),
                    field_name=f.field_name,
                    value_text=f.value_text,
                    value_normalized=f.value_normalized,
                    value_type=f.value_type,
                    confidence=f.confidence,
                    page_number=f.page_number,
                    evidence_excerpt=f.evidence_excerpt,
                    validation_status=f.validation_status,
                    grounding_status=f.grounding_status,
                    source=f.source,
                )
                for f in fields
                if f.document_id == d.id
            ],
        )
        for d in docs
    ]


async def prior_outputs(
    db: AsyncSession, claim_id: uuid.UUID, exclude: str | None = None
) -> dict[str, PriorOutput]:
    """Latest succeeded/partial output per agent on this claim."""
    rows = (
        await db.execute(
            select(AgentRun, AgentOutput)
            .join(AgentOutput, AgentOutput.agent_run_id == AgentRun.id)
            .where(AgentRun.claim_id == claim_id, AgentRun.status.in_(("succeeded", "partial")))
            .order_by(AgentRun.started_at.desc())
        )
    ).all()
    out: dict[str, PriorOutput] = {}
    for run, output in rows:
        if run.agent_key == exclude or run.agent_key in out:
            continue
        out[run.agent_key] = PriorOutput(
            agent_run_id=str(run.id),
            status=run.status,
            data=output.output,
            escalate=run.escalate,
            used_llm=run.used_llm,
        )
    return out


async def load(
    db: AsyncSession, organization_id: uuid.UUID, claim_id: uuid.UUID, agent_key: str, threshold: float
) -> ClaimSnapshot:
    claim = await db.get(Claim, claim_id)
    if claim is None or claim.organization_id != organization_id:
        raise LookupError("claim not found")
    customer = await db.get(Customer, claim.customer_id)
    policy = await db.get(Policy, claim.policy_id)
    product = await db.get(InsuranceProduct, claim.product_id)
    if customer is None or policy is None or product is None:
        raise LookupError("claim references are incomplete")
    rule_set = await rule_set_service.active(db, product.id)
    requirements = (
        await db.scalars(
            select(DocumentRequirement).where(DocumentRequirement.product_id == claim.product_id)
        )
    ).all()
    others = (
        await db.scalars(
            select(Claim).where(
                Claim.id != claim.id,
                Claim.deleted_at.is_(None),
                (Claim.policy_id == claim.policy_id) | (Claim.customer_id == claim.customer_id),
            )
        )
    ).all()
    numbers = (
        await db.execute(
            select(ExtractedField.value_text, Claim.id, Claim.claim_reference)
            .join(ClaimDocument, ClaimDocument.id == ExtractedField.document_id)
            .join(Claim, Claim.id == ClaimDocument.claim_id)
            .where(
                Claim.id != claim.id,
                ExtractedField.field_name.in_(("invoice_number", "estimate_number")),
                ExtractedField.is_current.is_(True),
                ExtractedField.value_text.is_not(None),
            )
            .limit(5000)
        )
    ).all()
    baseline = (
        await db.scalars(
            select(Claim.claimed_amount)
            .where(Claim.product_id == claim.product_id, Claim.id != claim.id, Claim.deleted_at.is_(None))
            .limit(BASELINE_LIMIT)
        )
    ).all()
    return ClaimSnapshot(
        organization_id=str(organization_id),
        claim=ClaimSnap(
            id=str(claim.id),
            claim_reference=claim.claim_reference,
            line=product.line,
            category=claim.category,
            status=claim.status,
            priority=claim.priority,
            incident_date=claim.incident_date,
            reported_at=claim.reported_at,
            description=claim.description,
            claimed_amount=claim.claimed_amount,
            currency=claim.currency,
            is_synthetic=claim.is_synthetic,
            policy_version_id=str(claim.policy_version_id) if claim.policy_version_id else None,
        ),
        customer=CustomerSnap(
            id=str(customer.id), full_name=customer.full_name, date_of_birth=customer.date_of_birth
        ),
        policy=PolicySnap(
            id=str(policy.id),
            policy_number=policy.policy_number,
            status=policy.status,
            versions=await _versions(db, policy.id),
        ),
        required_documents=[
            RequiredDoc(document_type=r.document_type, mandatory=r.mandatory) for r in requirements
        ],
        documents=await _documents(db, claim.id),
        customer_claims=[_ref(c) for c in others if c.customer_id == claim.customer_id],
        policy_claims=[_ref(c) for c in others if c.policy_id == claim.policy_id],
        other_invoices=[
            InvoiceRef(claim_id=str(cid), claim_reference=ref, invoice_number=n)
            for n, cid, ref in numbers
            if n
        ],
        amount_baseline=[Decimal(a) for a in baseline],
        usage_to_date=Decimal("0"),  # amounts paid against the policy arrive with decisions (Phase 9)
        prior_outputs=await prior_outputs(db, claim.id, exclude=agent_key),
        org_allows_real_data=await ai_service.org_allows_real_data(db, organization_id),
        confidence_threshold=threshold,
        rule_set_id=str(rule_set.id) if rule_set else None,
        rule_set_version=rule_set.version_no if rule_set else None,
        rule_set_config=dict(rule_set.config) if rule_set else None,
        today=datetime.now(UTC).date(),
    )
