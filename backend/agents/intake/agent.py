"""Intake agent: mandatory fields, duplicates, required documents, category and priority.

Deterministic. Duplicate rule (03 §2): same policy, incident date within ±3 days and a similar
amount (within 25%). Date-only matches are reported as information, not as duplicates.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from agents.base import Agent, AgentContext
from agents.contracts import (
    AgentResult,
    DuplicateClaim,
    EvidenceRef,
    Finding,
    IntakeOutput,
    MissingItem,
    RequiredDocumentStatus,
)
from agents.snapshot import ClaimRef, ClaimSnap, DocumentSnap, PolicySnap, RequiredDoc
from rules_engine import intake as intake_rules
from rules_engine.policy_validity import VersionTerms, select_version

SIMILAR_AMOUNT = Decimal("0.25")


def version_terms(policy: PolicySnap) -> list[VersionTerms]:
    return [
        VersionTerms(v.id, v.version_no, v.status, v.effective_from, v.effective_to, v.waiting_period_days)
        for v in policy.versions
    ]


def present_types(documents: list[DocumentSnap]) -> set[str]:
    return {
        d.document_type
        for d in documents
        if d.document_type and d.scan_status in ("clean", "skipped") and d.processing_status != "failed"
    }


def similar_amount(a: Decimal, b: Decimal) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= SIMILAR_AMOUNT * max(a, b)


class IntakeAgent(Agent):
    key: ClassVar[str] = "intake"
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {
            "claim.get",
            "policy.lookup",
            "product.document_requirements",
            "claims.find_similar_recent",
            "claim.documents",
            "clock.today",
        }
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        policy: PolicySnap = ctx.tools("policy.lookup")
        requirements: list[RequiredDoc] = ctx.tools("product.document_requirements")
        others: list[ClaimRef] = ctx.tools("claims.find_similar_recent")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        today = ctx.tools("clock.today")

        problems: list[str] = []
        if len(claim.description.strip()) < 10:
            problems.append("description is too short to assess the loss")
        if claim.claimed_amount <= 0:
            problems.append("claimed amount must be greater than zero")
        if claim.incident_date > today:
            problems.append("incident date is in the future")
        if claim.incident_date > claim.reported_at.date():
            problems.append("incident date is after the date the claim was reported")
        if claim.category not in intake_rules.LOSS_TYPES.get(claim.line, ()):
            problems.append(f"loss type '{claim.category}' is not valid for {claim.line} claims")

        lo, hi = intake_rules.duplicate_window(claim.incident_date)
        window = [
            c for c in others if c.id != claim.id and lo <= c.incident_date <= hi and c.status != "FAILED"
        ]
        duplicates = [c for c in window if similar_amount(c.claimed_amount, claim.claimed_amount)]

        version, _reason = select_version(version_terms(policy), claim.incident_date)
        chosen = next((v for v in policy.versions if version and v.id == version.id), None)
        terms = (
            intake_rules.PolicyTerms(
                chosen.effective_from,
                chosen.effective_to,
                chosen.sum_insured,
                chosen.currency,
                chosen.waiting_period_days,
            )
            if chosen
            else None
        )
        assessment = intake_rules.assess(
            incident_date=claim.incident_date,
            reported_on=claim.reported_at.date(),
            claimed_amount=claim.claimed_amount,
            currency=claim.currency,
            terms=terms,
            policy_status=policy.status,
            duplicate_references=[c.claim_reference for c in duplicates],
        )

        present = present_types(documents)
        statuses = [
            RequiredDocumentStatus(
                document_type=r.document_type, mandatory=r.mandatory, present=r.document_type in present
            )
            for r in requirements
        ]
        missing_docs = [s.document_type for s in statuses if s.mandatory and not s.present]

        claim_ref = EvidenceRef(kind="claim", id=claim.id)
        findings = [
            Finding(code=f.code, severity=f.severity, message=f.message, evidence=[claim_ref])
            for f in assessment.findings
        ]
        findings += [
            Finding(code="FIELD_PROBLEM", severity="error", message=p, evidence=[claim_ref]) for p in problems
        ]
        findings += [
            Finding(
                code="MISSING_DOCUMENT",
                severity="warning",
                message=f"Mandatory document not provided: {d}",
                evidence=[claim_ref],
            )
            for d in missing_docs
        ]
        date_only = [c for c in window if c not in duplicates]
        if date_only:
            refs = ", ".join(c.claim_reference for c in date_only)
            findings.append(
                Finding(
                    code="SAME_WINDOW_DIFFERENT_AMOUNT",
                    severity="info",
                    message=f"Other claims in the ±3 day window with different amounts: {refs}",
                )
            )

        output = IntakeOutput(
            claim_reference=claim.claim_reference,
            category=claim.category,
            priority=assessment.priority,
            mandatory_fields_ok=not problems,
            field_problems=problems,
            duplicates=[
                DuplicateClaim(
                    claim_id=c.id, claim_reference=c.claim_reference, incident_date=c.incident_date
                )
                for c in duplicates
            ],
            required_documents=statuses,
            missing_documents=missing_docs,
        )
        missing = [
            MissingItem(item=f"document: {d}", reason="mandatory for this product") for d in missing_docs
        ]
        data: dict[str, Any] = output.model_dump(mode="json")
        return AgentResult(status="succeeded", data=data, findings=findings, missing=missing)
