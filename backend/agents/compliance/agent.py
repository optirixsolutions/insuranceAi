"""Compliance agent (deterministic): is the recommendation safe to act on, and by whom?

Checks evidence and audit completeness, citation rules, monotonic escalation and the data
policy. ``automated_approval_allowed`` stays False until the STP gate and organisation profile
arrive (Phase 8); every recommendation is therefore human-reviewed in this phase.
"""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent, AgentContext, rule_config
from agents.contracts import AgentResult, ComplianceCheck, ComplianceOutput, Finding, MissingItem, Severity
from agents.snapshot import ClaimSnap, PriorOutput

REQUIRED_AGENTS = ("intake", "document", "policy", "fraud", "assessment", "validation", "decision")
RANK = {
    "APPROVE_RECOMMENDED": 0,
    "REJECT_RECOMMENDED": 0,
    "PENDING_VERIFICATION": 1,
    "MORE_INFORMATION_REQUIRED": 1,
    "MANUAL_REVIEW_REQUIRED": 2,
}


class ComplianceAgent(Agent):
    key: ClassVar[str] = "compliance"
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {"claim.get", "agents.prior_outputs", "org.ai_data_approval"}
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        prior: dict[str, PriorOutput] = ctx.tools("agents.prior_outputs")
        ai_approved: bool = ctx.tools("org.ai_data_approval")
        checks: list[ComplianceCheck] = []

        def check(code: str, passed: bool, severity: Severity, message: str) -> None:
            checks.append(ComplianceCheck(code=code, passed=passed, severity=severity, message=message))

        missing_agents = [k for k in REQUIRED_AGENTS if k not in prior or prior[k].data is None]
        check(
            "C-AGENT-COVERAGE",
            not missing_agents,
            "error",
            "All analyses present"
            if not missing_agents
            else f"Missing analyses: {', '.join(missing_agents)}",
        )

        policy = (prior.get("policy") or PriorOutput(agent_run_id="", status="", data=None)).data or {}
        check(
            "C-POLICY-VERSION",
            bool(policy.get("policy_version_id")),
            "error",
            "Policy version recorded"
            if policy.get("policy_version_id")
            else "No policy version recorded for the incident date",
        )
        uncited = [c["coverage_code"] for c in policy.get("coverages", []) if not c.get("clause_ids")]
        check(
            "C-COVERAGE-CITATION",
            not uncited,
            "critical",
            "Every coverage determination cites a clause" if not uncited else f"Uncited coverage: {uncited}",
        )

        intake = (prior.get("intake") or PriorOutput(agent_run_id="", status="", data=None)).data or {}
        docs_missing = intake.get("missing_documents", [])
        check(
            "C-EVIDENCE-COMPLETE",
            not docs_missing,
            "warning",
            "Mandatory documents present"
            if not docs_missing
            else f"Mandatory documents missing: {', '.join(docs_missing)}",
        )

        fraud = (prior.get("fraud") or PriorOutput(agent_run_id="", status="", data=None)).data or {}
        unsupported = [i["rule_code"] for i in fraud.get("indicators", []) if not i.get("evidence")]
        check(
            "C-FRAUD-EVIDENCE",
            not unsupported,
            "critical",
            "Every fraud indicator cites evidence"
            if not unsupported
            else f"Indicators without evidence: {unsupported}",
        )

        decision = (prior.get("decision") or PriorOutput(agent_run_id="", status="", data=None)).data or {}
        rec = decision.get("recommendation")
        det = decision.get("deterministic_outcome")
        if rec and det:
            upgraded = RANK.get(rec, 0) < RANK.get(det, 0) or (
                rec != det and rec in ("APPROVE_RECOMMENDED", "REJECT_RECOMMENDED")
            )
            check(
                "C-ESCALATION-MONOTONIC",
                not upgraded,
                "critical",
                "Model review only escalated"
                if not upgraded
                else f"Recommendation {rec} departs from deterministic {det} in a non-escalating direction",
            )
            needs_evidence = rec in ("APPROVE_RECOMMENDED", "REJECT_RECOMMENDED")
            explained = bool(decision.get("explanation")) and (
                not needs_evidence or bool(decision.get("supporting_evidence"))
            )
            check(
                "C-EXPLANATION",
                explained,
                "error",
                "Recommendation is explained with evidence"
                if explained
                else "Recommendation lacks an explanation or supporting evidence",
            )

        used_llm = [k for k, p in prior.items() if p.used_llm]
        privacy_ok = claim.is_synthetic or ai_approved or not used_llm
        check(
            "C-DATA-POLICY",
            privacy_ok,
            "critical",
            "AI processing permitted for this data"
            if privacy_ok
            else f"Real claim data sent to a model without organisation approval ({used_llm})",
        )

        config = rule_config(ctx.snapshot)
        profile_allows = config.automated_approval_allowed and config.stp.enabled
        # Eligibility only: the STP gate is re-evaluated from persisted data before anything is paid.
        escalated = decision.get("llm_escalated") is not False
        human_required = not (profile_allows and rec == "APPROVE_RECOMMENDED" and not escalated)
        check(
            "C-HUMAN-REVIEW",
            True,
            "info",
            f"Human review required ({rec or 'no recommendation'})"
            if human_required
            else "Eligible for the straight-through gate (re-checked before approval)",
        )

        failed = [c for c in checks if not c.passed]
        if any(c.severity in ("error", "critical") for c in failed):
            status = "non_compliant"
        elif failed or human_required:
            status = "review_required"
        else:
            status = "compliant"
        output = ComplianceOutput(
            status=status,
            checks=checks,
            human_review_required=human_required,
            automated_approval_allowed=profile_allows,
        )
        findings = [Finding(code=c.code, severity=c.severity, message=c.message) for c in failed]
        missing = [
            MissingItem(item=f"analysis: {a}", reason="required before a decision") for a in missing_agents
        ]
        return AgentResult(
            status="succeeded",
            data=output.model_dump(mode="json"),
            findings=findings,
            missing=missing,
            escalate=status == "non_compliant",
        )
