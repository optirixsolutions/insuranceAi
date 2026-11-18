"""Decision recommendation agent.

The outcome is the deterministic matrix (rules_engine.decision). The model (``reasoning``) writes
the explanation and may *escalate* approve/reject to manual review, citing evidence ids from the
index it is given; it can never upgrade. Without a model a templated explanation is used.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent, AgentContext
from agents.contracts import AgentResult, DecisionRecommendationOutput, EvidenceRef, Finding, MissingItem
from agents.snapshot import ClaimSnap, DocumentSnap, PriorOutput
from rules_engine.decision import DecisionInputs, Recommendation, apply_escalation, evaluate

INPUT_AGENTS = ("intake", "document", "policy", "fraud", "assessment", "validation")
_AMOUNT = re.compile(r"\d[\d,]*\.\d{2}")


class LlmDecisionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(description="Plain-language explanation for a claims handler, max 120 words")
    escalate: bool = Field(
        description="True only if the evidence shows a reason for human review the rules missed"
    )
    escalation_reasons: list[str]
    evidence_ids: list[str] = Field(
        description="Ids from the evidence index supporting the explanation or escalation"
    )


def _d(prior: dict[str, PriorOutput], key: str) -> dict[str, Any]:
    p = prior.get(key)
    return (p.data or {}) if p and p.status in ("succeeded", "partial") else {}


def build_inputs(prior: dict[str, PriorOutput], documents: list[DocumentSnap]) -> DecisionInputs:
    intake, policy, assessment = _d(prior, "intake"), _d(prior, "policy"), _d(prior, "assessment")
    document, validation, fraud = _d(prior, "document"), _d(prior, "validation"), _d(prior, "fraud")
    low = sorted(
        {
            f"{r['filename']}: {n}"
            for r in document.get("documents", [])
            for n in r.get("needs_human_verification", [])
        }
    )
    contradictions = list(validation.get("contradictions", []))
    if document.get("injection_suspected") or any(
        i.get("rule_code") == "F-INJECTION" for i in fraud.get("indicators", [])
    ):
        contradictions.append("documents contain instruction-like text aimed at automated systems")
    return DecisionInputs(
        missing_required_documents=tuple(intake.get("missing_documents", []))
        if intake
        else ("(intake not run)",),
        policy_version_resolved=bool(policy.get("policy_version_id")),
        assessment_status=assessment.get("status"),
        low_confidence_fields=tuple(low),
        av_scan_pending=any(d.scan_status in ("pending", "scanning") for d in documents),
        coverage_statuses={c["coverage_code"]: c["status"] for c in policy.get("coverages", [])},
        primary_coverage=policy.get("primary_coverage"),
        exclusions_need_interpretation=tuple(
            e["exclusion_code"] for e in policy.get("exclusions_need_interpretation", [])
        ),
        exclusions_triggered_with_clause=tuple(
            e["exclusion_code"] for e in policy.get("exclusions_triggered", []) if e.get("clause_id")
        ),
        validation_max_severity=validation.get("max_severity"),
        fraud_level=fraud.get("risk_level"),
        contradictions=tuple(contradictions),
        policy_active=policy.get("policy_active_on_incident_date") if policy else None,
        payout=str(assessment["eligible_payout"]) if assessment.get("status") == "calculated" else None,
    )


def evidence_index(prior: dict[str, PriorOutput]) -> tuple[list[EvidenceRef], list[EvidenceRef]]:
    policy, assessment, validation, fraud = (
        _d(prior, k) for k in ("policy", "assessment", "validation", "fraud")
    )
    support: list[EvidenceRef] = []
    for c in policy.get("coverages", []):
        support += [
            EvidenceRef(kind="clause", id=cid, excerpt=f"{c['coverage_code']}: {c['status']}")
            for cid in c.get("clause_ids", [])
        ]
    for e in policy.get("exclusions_triggered", []):
        support.append(
            EvidenceRef(
                kind="clause",
                id=e["clause_id"],
                excerpt=f"exclusion {e['exclusion_code']}: {e['reason']}"[:500],
            )
        )
    for li in assessment.get("line_items", []):
        support += [EvidenceRef(**e) for e in li.get("evidence", [])][:1]
    contra: list[EvidenceRef] = []
    for f in validation.get("findings", []):
        if f["severity"] in ("error", "critical"):
            contra += [EvidenceRef(**e) for e in f.get("evidence", [])][:2]
    for i in fraud.get("indicators", []):
        contra += [EvidenceRef(**e) for e in i.get("evidence", [])][:2]
    return support[:40], contra[:40]


def template(
    outcome: Recommendation,
    rule: str,
    reasons: list[str],
    missing: list[str],
    payout: str | None,
    currency: str,
) -> str:
    base = {
        Recommendation.APPROVE: (
            "The loss is covered, checks are clean and the fraud risk is low; "
            f"the calculated payout is {payout} {currency}."
        ),
        Recommendation.REJECT: (
            "The rules indicate the claim is not payable; a handler must confirm before any rejection."
        ),
        Recommendation.MANUAL_REVIEW: "A handler must review this claim before a decision.",
        Recommendation.MORE_INFO: "The claim cannot be assessed until more information is provided.",
        Recommendation.PENDING_VERIFICATION: (
            "Some evidence must be verified before the claim can be assessed."
        ),
    }[outcome]
    detail = reasons or missing
    return f"{base} Matrix rule {rule}." + (f" Reasons: {'; '.join(detail[:6])}." if detail else "")


class DecisionAgent(Agent):
    key: ClassVar[str] = "decision"
    uses_llm: ClassVar[bool] = True
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {"claim.get", "agents.prior_outputs", "claim.documents"}
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        prior: dict[str, PriorOutput] = ctx.tools("agents.prior_outputs")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        inputs = build_inputs(prior, documents)
        matrix = evaluate(inputs)
        support, contra = evidence_index(prior)
        index = {e.id: e for e in [*support, *contra]}
        facts_text = json.dumps({k: _d(prior, k) for k in INPUT_AGENTS}, default=str)

        def validate(r: LlmDecisionReview) -> list[str]:
            problems = [f"evidence id '{i}' is not in the index" for i in r.evidence_ids if i not in index]
            for amount in _AMOUNT.findall(r.explanation):
                if amount.replace(",", "") not in facts_text:
                    problems.append(f"amount {amount} in the explanation is not in the analysis")
            named = {v.value for v in Recommendation if v.value in r.explanation}
            if named - {matrix.outcome.value, Recommendation.MANUAL_REVIEW.value}:
                problems.append("the explanation names a different recommendation than the rules produced")
            if r.escalate and not r.evidence_ids:
                problems.append("an escalation must cite evidence ids")
            return problems

        review = None
        if any(_d(prior, k) for k in INPUT_AGENTS):
            brief = {
                "deterministic_outcome": matrix.outcome.value,
                "matrix_rule": matrix.rule,
                "applied_rules": matrix.applied_rules,
                "mandatory_review_reasons": matrix.mandatory_review_reasons,
                "missing_information": matrix.missing_information,
                "evidence_index": [e.model_dump(exclude_none=True) for e in index.values()][:60],
            }
            analyses = {k: _d(prior, k) for k in INPUT_AGENTS}
            user = (
                f"<decision_brief>\n{json.dumps(brief, default=str)}\n</decision_brief>\n"
                f"<analyses>\n{json.dumps(analyses, default=str)[:12000]}\n</analyses>"
            )
            review = await self.ask(ctx, LlmDecisionReview, user, validate)

        final, escalated = apply_escalation(
            matrix.outcome, bool(review and review.escalate), len(review.evidence_ids) if review else 0
        )
        reasons = list(matrix.mandatory_review_reasons)
        esc_reasons = [r[:300] for r in review.escalation_reasons] if review and escalated else []
        if escalated:
            reasons += [f"model escalation: {r}" for r in esc_reasons]
        explanation = (
            review.explanation.strip()[:1500]
            if review
            else template(
                matrix.outcome,
                matrix.rule,
                reasons,
                matrix.missing_information,
                inputs.payout,
                claim.currency,
            )
        )
        if escalated:
            explanation += " Escalated to manual review by the model review step."
        cited = [index[i] for i in (review.evidence_ids if review else []) if i in index]
        out = DecisionRecommendationOutput(
            recommendation=final.value,
            deterministic_outcome=matrix.outcome.value,
            matrix_rule=matrix.rule,
            llm_escalated=escalated,
            escalation_reasons=esc_reasons,
            explanation=explanation,
            explanation_source="llm" if review else "template",
            applied_rules=matrix.applied_rules,
            supporting_evidence=cited or support[:20],
            contradictory_evidence=contra[:20],
            missing_information=matrix.missing_information,
            mandatory_review_reasons=reasons,
            inputs_from={k: (prior[k].agent_run_id if k in prior else None) for k in INPUT_AGENTS},
        )
        absent = [k for k in INPUT_AGENTS if k not in prior]
        missing = [MissingItem(item=f"analysis: {k}", reason="run this agent first") for k in absent]
        findings = [Finding(code="RECOMMENDATION", severity="info", message=f"{final.value} ({matrix.rule})")]
        return AgentResult(
            status="partial" if absent else "succeeded",
            data=out.model_dump(mode="json"),
            findings=findings,
            missing=missing,
            used_llm=review is not None,
            escalate=escalated or final != Recommendation.APPROVE,
        )
