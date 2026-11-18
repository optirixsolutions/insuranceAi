"""The claim processing graph (02-workflow.md §2).

INTAKE -> DOCUMENT_PROCESSING -> DOCUMENT_VALIDATION -> (DOCUMENT_REVIEW interrupt) ->
fan-out [POLICY_VERIFICATION -> CLAIM_ASSESSMENT] || [FRAUD_ANALYSIS] -> join -> RESULT_AGGREGATION
-> CROSS_VALIDATION -> DECISION_RECOMMENDATION -> COMPLIANCE_CHECK -> HUMAN_REVIEW or MORE_INFO
(interrupts). A reviewer may return the claim for reassessment (CLAIM_REASSESSMENT, then straight
to aggregation: the fraud branch is not re-run, so the join is not waited on again) or request
information (MORE_INFO -> INTAKE). Approve/reject and finalisation arrive with Phase 9.

Deviation from the design: fraud amount checks (stage 2) need no assessed line items in this
implementation (the fraud agent reads them from the documents), so stage 2 is part of FRAUD_ANALYSIS.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from rules_engine.claim_status import ClaimStatus as S
from workflows import nodes as n
from workflows import review, stp
from workflows.state import ClaimState

RETRY = RetryPolicy(
    max_attempts=2, initial_interval=2.0, backoff_factor=2.0, jitter=True, retry_on=n.retryable
)


def build() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(ClaimState)
    add = g.add_node
    add(
        "INTAKE",
        n.wrap("INTAKE", n.intake, agent="intake"),
        retry_policy=RETRY,
    )
    add(
        "DOCUMENT_PROCESSING",
        n.wrap("DOCUMENT_PROCESSING", n.wait_for_documents, status=S.DOCUMENT_PROCESSING, agent="document"),
        retry_policy=RETRY,
    )
    add("DOCUMENT_VALIDATION", n.wrap("DOCUMENT_VALIDATION", n.validate_documents))
    add("DOCUMENT_REVIEW", n.wrap("DOCUMENT_REVIEW", n.document_review))
    add(
        "POLICY_VERIFICATION",
        n.wrap(
            "POLICY_VERIFICATION",
            n.agent_step("policy", "POLICY_VERIFICATION"),
            status=S.POLICY_VERIFICATION,
            agent="policy",
        ),
        retry_policy=RETRY,
    )
    add(
        "FRAUD_ANALYSIS",
        n.wrap("FRAUD_ANALYSIS", n.agent_step("fraud", "FRAUD_ANALYSIS"), agent="fraud"),
        retry_policy=RETRY,
    )
    add(
        "CLAIM_ASSESSMENT",
        n.wrap(
            "CLAIM_ASSESSMENT",
            n.agent_step("assessment", "CLAIM_ASSESSMENT"),
            status=S.CLAIM_ASSESSMENT,
            agent="assessment",
        ),
        retry_policy=RETRY,
    )
    add(
        "CLAIM_REASSESSMENT",
        n.wrap(
            "CLAIM_REASSESSMENT",
            n.agent_step("assessment", "CLAIM_REASSESSMENT"),
            status=S.CLAIM_ASSESSMENT,
            agent="assessment",
        ),
        retry_policy=RETRY,
    )
    add("RESULT_AGGREGATION", n.wrap("RESULT_AGGREGATION", n.aggregate))
    add(
        "CROSS_VALIDATION",
        n.wrap(
            "CROSS_VALIDATION",
            n.agent_step("validation", "CROSS_VALIDATION"),
            status=S.VALIDATION,
            agent="validation",
        ),
        retry_policy=RETRY,
    )
    add(
        "DECISION_RECOMMENDATION",
        n.wrap(
            "DECISION_RECOMMENDATION",
            n.agent_step("decision", "DECISION_RECOMMENDATION"),
            status=S.DECISION_PENDING,
            agent="decision",
        ),
        retry_policy=RETRY,
    )
    add("COMPLIANCE_CHECK", n.wrap("COMPLIANCE_CHECK", n.compliance, agent="compliance"), retry_policy=RETRY)
    add("MORE_INFO", n.wrap("MORE_INFO", n.more_info))
    add("HUMAN_REVIEW", n.wrap("HUMAN_REVIEW", review.human_review))

    add("AUTO_APPROVAL_ELIGIBILITY", n.wrap("AUTO_APPROVAL_ELIGIBILITY", stp.eligibility))
    add("FINALIZATION", n.wrap("FINALIZATION", stp.finalize))
    g.add_edge(START, "INTAKE")
    g.add_conditional_edges(
        "INTAKE", n.by_route, {"documents": "DOCUMENT_PROCESSING", "more_info": "MORE_INFO"}
    )
    g.add_edge("DOCUMENT_PROCESSING", "DOCUMENT_VALIDATION")
    g.add_conditional_edges(
        "DOCUMENT_VALIDATION",
        lambda s: (
            ["POLICY_VERIFICATION", "FRAUD_ANALYSIS"] if n.by_route(s) == "continue" else "DOCUMENT_REVIEW"
        ),
        ["POLICY_VERIFICATION", "FRAUD_ANALYSIS", "DOCUMENT_REVIEW"],
    )
    g.add_conditional_edges(
        "DOCUMENT_REVIEW", n.by_route, {"reprocess": "DOCUMENT_PROCESSING", "continue": "DOCUMENT_VALIDATION"}
    )
    g.add_edge("POLICY_VERIFICATION", "CLAIM_ASSESSMENT")
    g.add_edge(["FRAUD_ANALYSIS", "CLAIM_ASSESSMENT"], "RESULT_AGGREGATION")
    g.add_edge("CLAIM_REASSESSMENT", "RESULT_AGGREGATION")
    g.add_edge("RESULT_AGGREGATION", "CROSS_VALIDATION")
    g.add_edge("CROSS_VALIDATION", "DECISION_RECOMMENDATION")
    g.add_edge("DECISION_RECOMMENDATION", "COMPLIANCE_CHECK")
    g.add_conditional_edges(
        "COMPLIANCE_CHECK",
        n.by_route,
        {"more_info": "MORE_INFO", "human_review": "HUMAN_REVIEW", "stp": "AUTO_APPROVAL_ELIGIBILITY"},
    )
    g.add_conditional_edges("MORE_INFO", n.by_route, {"intake": "INTAKE", "human_review": "HUMAN_REVIEW"})
    g.add_conditional_edges(
        "HUMAN_REVIEW", n.by_route, {"reassess": "CLAIM_REASSESSMENT", "more_info": "MORE_INFO", "done": END}
    )
    g.add_conditional_edges(
        "AUTO_APPROVAL_ELIGIBILITY", n.by_route, {"finalize": "FINALIZATION", "human_review": "HUMAN_REVIEW"}
    )
    g.add_edge("FINALIZATION", END)
    return g
