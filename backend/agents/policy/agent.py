"""Policy verification agent.

Deterministic: version in force on the incident date, validity, waiting period (from first
inception), peril-based coverage with clause citations, machine-checkable exclusions, deductible
and limit remaining. Model (``reasoning``): maps the narrative to perils and *flags* exclusions
needing interpretation, citing a grounded excerpt. The model can only make the outcome more
cautious (covered -> potentially_covered, not_triggered -> needs_interpretation), never less.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent, AgentContext
from agents.contracts import (
    AgentResult,
    CoverageDetermination,
    EvidenceRef,
    ExclusionFinding,
    Finding,
    MissingItem,
    Money,
    PolicyVerificationOutput,
)
from agents.intake.agent import present_types, version_terms
from agents.snapshot import ClaimSnap, CustomerSnap, DocumentSnap, PolicySnap, VersionSnap
from rules_engine import intake as intake_rules
from rules_engine.coverage import (
    CoverageTerms,
    ExclusionTerms,
    Facts,
    evaluate_coverages,
    evaluate_exclusions,
)
from rules_engine.policy_validity import is_active, select_version, waiting_period_ok
from rules_engine.text import excerpt_in_text, name_similarity, names_match

MAX_DOC_CHARS = 1500


class LlmExclusionConcern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclusion_code: str
    evidence_excerpt: str = Field(description="Verbatim text from the loss description or a document")
    reason: str


class LlmPolicyReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    loss_perils: list[str] = Field(description="Perils from the allowed list that describe the loss")
    exclusion_concerns: list[LlmExclusionConcern] = Field(
        description="Exclusions the evidence may trigger; empty if none"
    )


def holder_match(
    customer: CustomerSnap, documents: list[DocumentSnap]
) -> Literal["match", "partial", "mismatch", "unknown"]:
    names = [
        f.value_text
        for d in documents
        for f in d.fields
        if f.field_name in ("policyholder_name", "patient_name") and f.value_text
    ]
    if not names:
        return "unknown"
    if all(names_match(n, customer.full_name) for n in names):
        return "match"
    if all(name_similarity(n, customer.full_name) >= 0.80 for n in names):
        return "partial"
    return "mismatch"


def facts_for(claim: ClaimSnap, documents: list[DocumentSnap]) -> Facts:
    fields: dict[str, str] = {}
    for d in documents:
        for f in d.fields:
            if (
                f.validation_status != "rejected"
                and f.field_name not in fields
                and f.value_normalized is not None
            ):
                fields[f.field_name] = str(f.value_normalized)
    return Facts(
        claim.category, claim.description, claim.incident_date, frozenset(present_types(documents)), fields
    )


class PolicyAgent(Agent):
    key: ClassVar[str] = "policy"
    uses_llm: ClassVar[bool] = True
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {
            "claim.get",
            "customer.get",
            "policy.lookup",
            "policy.version_for_date",
            "policy.usage_to_date",
            "claim.documents",
        }
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        customer: CustomerSnap = ctx.tools("customer.get")
        policy: PolicySnap = ctx.tools("policy.lookup")
        usage: Decimal = ctx.tools("policy.usage_to_date")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        terms = version_terms(policy)
        chosen_terms, reason = select_version(terms, claim.incident_date)
        version: VersionSnap | None = next(
            (v for v in policy.versions if chosen_terms and v.id == chosen_terms.id), None
        )
        holder = holder_match(customer, documents)
        findings: list[Finding] = []
        if holder == "mismatch":
            findings.append(
                Finding(
                    code="HOLDER_MISMATCH",
                    severity="error",
                    message="Name on the documents does not match the policyholder",
                )
            )
        if version is None or chosen_terms is None:
            out = PolicyVerificationOutput(
                policy_version_id=None,
                version_selection_reason=reason,
                holder_match=holder,
                policy_active_on_incident_date=False,
                waiting_period_satisfied=None,
                loss_type=claim.category,
                model_perils=[],
                coverages=[],
                primary_coverage=None,
                exclusions_triggered=[],
                exclusions_need_interpretation=[],
                exclusions_not_triggered=[],
                deductible=None,
                limit_remaining=None,
            )
            findings.append(
                Finding(
                    code="NO_VERSION_IN_FORCE",
                    severity="error",
                    message=reason,
                    evidence=[EvidenceRef(kind="claim", id=claim.id)],
                )
            )
            return AgentResult(
                status="partial",
                data=out.model_dump(mode="json"),
                findings=findings,
                missing=[MissingItem(item="policy version in force", reason=reason)],
            )

        active = is_active(policy.status, chosen_terms, claim.incident_date)
        waiting_ok = waiting_period_ok(chosen_terms, terms, claim.incident_date)
        clause_id = {c.clause_ref: c.id for c in version.clauses}
        cov_terms = [CoverageTerms(c.coverage_code, tuple(c.perils), c.clause_ref) for c in version.coverages]
        outcomes = evaluate_coverages(cov_terms, claim.category)

        # Model review: perils for the narrative, and exclusion concerns (escalation only).
        source_text = "\n".join([claim.description, *(p.text for d in documents for p in d.pages)])
        allowed_perils = sorted(
            {p for c in version.coverages for p in c.perils}
            | set(intake_rules.LOSS_TYPES.get(claim.line, ()))
        )
        covered_codes = {
            o.coverage_code
            for o in outcomes
            if o.status in ("covered", "potentially_covered", "requires_manual_interpretation")
        }
        excl_terms = [
            ExclusionTerms(e.exclusion_code, tuple(e.applies_to), e.description, e.clause_ref)
            for e in version.exclusions
        ]
        relevant_codes = {e.exclusion_code for e in excl_terms if covered_codes.intersection(e.applies_to)}

        def validate(r: LlmPolicyReview) -> list[str]:
            problems = [
                f"peril '{p}' is not in the allowed list" for p in r.loss_perils if p not in allowed_perils
            ]
            for c in r.exclusion_concerns:
                if c.exclusion_code not in relevant_codes:
                    problems.append(f"exclusion '{c.exclusion_code}' is not one of {sorted(relevant_codes)}")
                elif not excerpt_in_text(c.evidence_excerpt, source_text):
                    problems.append(f"evidence for '{c.exclusion_code}' is not verbatim text from the claim")
            return problems

        review = None
        if relevant_codes or claim.category == "other":
            payload = {
                "loss_type_on_claim": claim.category,
                "allowed_perils": allowed_perils,
                "exclusions": [
                    {
                        "exclusion_code": e.exclusion_code,
                        "description": e.description,
                        "clause_text": next(
                            (c.text for c in version.clauses if c.clause_ref == e.clause_ref), ""
                        ),
                    }
                    for e in version.exclusions
                    if e.exclusion_code in relevant_codes
                ],
            }
            evidence = "\n\n".join(
                f'<document name="{d.filename}">\n'
                f"{' '.join(p.text for p in d.pages)[:MAX_DOC_CHARS]}\n</document>"
                for d in documents
            )
            user = (
                f"<policy_terms>\n{json.dumps(payload)}\n</policy_terms>\n<loss_description>\n{claim.description}\n</loss_description>\n"
                f"<documents>\n{evidence}\n</documents>"
            )
            review = await self.ask(ctx, LlmPolicyReview, user, validate)

        coverages: list[CoverageDetermination] = []
        for o in outcomes:
            status = o.status
            reasons = list(o.reasons)
            if status == "covered" and not waiting_ok:
                status, reasons = "not_covered", [*reasons, "WITHIN_WAITING_PERIOD"]
            if (
                status == "covered"
                and review
                and review.loss_perils
                and claim.category not in review.loss_perils
            ):
                status, reasons = "potentially_covered", [*reasons, "MODEL_READS_LOSS_DIFFERENTLY"]
            cid = clause_id.get(o.clause_ref)
            if cid is None:  # fail closed: no clause text, no determination
                status, reasons, cid = (
                    "requires_manual_interpretation",
                    [*reasons, "CLAUSE_TEXT_MISSING"],
                    f"{version.id}:{o.clause_ref}",
                )
            coverages.append(
                CoverageDetermination(
                    coverage_code=o.coverage_code, status=status, reasons=reasons, clause_ids=[cid]
                )
            )

        concerns = {c.exclusion_code: c for c in (review.exclusion_concerns if review else [])}
        triggered: list[ExclusionFinding] = []
        interpret: list[ExclusionFinding] = []
        clear: list[ExclusionFinding] = []
        for e in evaluate_exclusions(excl_terms, covered_codes, facts_for(claim, documents)):
            cid = clause_id.get(e.clause_ref, f"{version.id}:{e.clause_ref}")
            concern = concerns.get(e.exclusion_code)
            if e.status == "triggered":
                triggered.append(
                    ExclusionFinding(
                        exclusion_code=e.exclusion_code,
                        clause_id=cid,
                        reason=e.reason,
                        source="rule",
                        evidence_excerpt=e.evidence,
                    )
                )
            elif e.status == "needs_interpretation":
                interpret.append(
                    ExclusionFinding(
                        exclusion_code=e.exclusion_code,
                        clause_id=cid,
                        reason=e.reason,
                        source="rule",
                        evidence_excerpt=e.evidence,
                    )
                )
            elif concern is not None:
                interpret.append(
                    ExclusionFinding(
                        exclusion_code=e.exclusion_code,
                        clause_id=cid,
                        reason=concern.reason[:300],
                        source="llm",
                        evidence_excerpt=concern.evidence_excerpt[:500],
                    )
                )
            elif e.status == "unevaluated" and review is None:
                interpret.append(
                    ExclusionFinding(
                        exclusion_code=e.exclusion_code,
                        clause_id=cid,
                        reason="No machine-checkable rule and no model review available",
                        source="rule",
                    )
                )
            elif e.status == "unevaluated":
                clear.append(
                    ExclusionFinding(
                        exclusion_code=e.exclusion_code,
                        clause_id=cid,
                        reason="Model review found no indication in the evidence",
                        source="llm",
                    )
                )
            else:
                clear.append(
                    ExclusionFinding(
                        exclusion_code=e.exclusion_code, clause_id=cid, reason=e.reason, source="rule"
                    )
                )

        primary = next(
            (c.coverage_code for c in coverages if c.status in ("covered", "potentially_covered")), None
        )
        primary_cov = next((c for c in version.coverages if c.coverage_code == primary), None)
        deductible = (
            primary_cov.deductible_override
            if primary_cov and primary_cov.deductible_override is not None
            else version.deductible_amount
        )
        cap = (
            version.annual_aggregate_limit
            if claim.line == "health" and version.annual_aggregate_limit
            else (primary_cov.limit_amount if primary_cov else version.sum_insured)
        )
        remaining = max(Decimal("0"), min(cap, version.sum_insured) - usage)
        out = PolicyVerificationOutput(
            policy_version_id=version.id,
            version_selection_reason=reason,
            holder_match=holder,
            policy_active_on_incident_date=active,
            waiting_period_satisfied=waiting_ok,
            loss_type=claim.category,
            model_perils=review.loss_perils if review else [],
            coverages=coverages,
            primary_coverage=primary,
            exclusions_triggered=triggered,
            exclusions_need_interpretation=interpret,
            exclusions_not_triggered=clear,
            deductible=Money(amount=deductible, currency=version.currency),
            limit_remaining=Money(amount=remaining, currency=version.currency),
        )
        if not active:
            findings.append(
                Finding(
                    code="POLICY_NOT_ACTIVE",
                    severity="error",
                    message=f"Policy is not active on {claim.incident_date}",
                )
            )
        if not waiting_ok:
            findings.append(
                Finding(
                    code="WITHIN_WAITING_PERIOD",
                    severity="error",
                    message="Incident falls within the waiting period",
                )
            )
        findings += [
            Finding(code="EXCLUSION_TRIGGERED", severity="error", message=f"{t.exclusion_code}: {t.reason}")
            for t in triggered
        ]
        findings += [
            Finding(
                code="EXCLUSION_NEEDS_INTERPRETATION",
                severity="warning",
                message=f"{t.exclusion_code}: {t.reason}",
            )
            for t in interpret
        ]
        note = None
        if ctx.prompt and review is None and (relevant_codes or claim.category == "other"):
            note = "Model review unavailable: unevaluated exclusions were sent for interpretation"
        return AgentResult(
            status="succeeded",
            data=out.model_dump(mode="json"),
            findings=findings,
            used_llm=review is not None,
            escalate=bool(interpret),
            llm_note=note,
        )
