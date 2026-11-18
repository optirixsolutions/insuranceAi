"""Evaluation harness: run agents over labelled cases, compute metrics, compare to thresholds."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents.base import LlmClient
from agents.contracts import AgentResult
from agents.registry import ORDER, get
from agents.snapshot import PriorOutput
from evaluations.builders import Case


@dataclass
class Metric:
    name: str
    value: float
    threshold: float | None = None  # None = reported, not gated
    higher_is_better: bool = True

    @property
    def passed(self) -> bool:
        if self.threshold is None:
            return True
        return self.value >= self.threshold if self.higher_is_better else self.value <= self.threshold

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "threshold": self.threshold,
            "higher_is_better": self.higher_is_better,
            "passed": self.passed,
        }


@dataclass
class EvalResult:
    agent: str
    cases: int
    metrics: list[Metric] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mode: str = "deterministic"

    @property
    def passed(self) -> bool:
        return all(m.passed for m in self.metrics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "cases": self.cases,
            "mode": self.mode,
            "passed": self.passed,
            "metrics": [m.as_dict() for m in self.metrics],
            "failures": self.failures[:50],
            "notes": self.notes,
        }


LlmSource = LlmClient | Callable[[str], LlmClient | None] | None


def llm_for(source: LlmSource, agent_key: str) -> LlmClient | None:
    """A single client, or a per-agent factory (live runs use each agent's own model configuration)."""
    if source is None or hasattr(source, "structured"):
        return source
    return source(agent_key)


def ratio(hits: int, total: int) -> float:
    return 1.0 if total == 0 else hits / total


async def run_chain(
    case: Case, keys: tuple[str, ...] = ORDER, llm: LlmSource = None
) -> dict[str, AgentResult]:
    """Run agents in pipeline order, feeding each one's output to the next (as the service does)."""
    results: dict[str, AgentResult] = {}
    prior = dict(case.prior)
    for key in keys:
        case.prior = prior
        agent = get(key)
        result = await agent.run(agent.context(case.snapshot(), llm_for(llm, key)))
        results[key] = result
        if result.status in ("succeeded", "partial"):
            prior[key] = PriorOutput(
                agent_run_id=f"eval-{key}",
                status=result.status,
                data=result.data,
                escalate=result.escalate,
                used_llm=result.used_llm,
            )
    case.prior = prior
    return results


async def run_one(case: Case, key: str, llm: LlmSource = None) -> AgentResult:
    agent = get(key)
    return await agent.run(agent.context(case.snapshot(), llm_for(llm, key)))
