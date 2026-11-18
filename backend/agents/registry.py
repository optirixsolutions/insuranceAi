"""The eight Phase 6 agents, in the order the Phase 7 graph will run them.

The supervisor is orchestration only (ADR-001) and arrives with LangGraph in Phase 7.
"""

from __future__ import annotations

from agents.assessment.agent import AssessmentAgent
from agents.base import Agent
from agents.compliance.agent import ComplianceAgent
from agents.decision.agent import DecisionAgent
from agents.document.agent import DocumentAgent
from agents.fraud.agent import FraudAgent
from agents.intake.agent import IntakeAgent
from agents.policy.agent import PolicyAgent
from agents.validation.agent import ValidationAgent

AGENTS: dict[str, type[Agent]] = {
    a.key: a
    for a in (
        IntakeAgent,
        DocumentAgent,
        PolicyAgent,
        FraudAgent,
        AssessmentAgent,
        ValidationAgent,
        DecisionAgent,
        ComplianceAgent,
    )
}
ORDER: tuple[str, ...] = tuple(AGENTS)
LLM_AGENTS: tuple[str, ...] = tuple(k for k, a in AGENTS.items() if a.uses_llm)


def get(key: str) -> Agent:
    return AGENTS[key]()
