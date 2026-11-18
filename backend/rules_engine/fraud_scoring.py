"""Fraud scoring framework v1 (03-agent-contracts.md §6). Explainable, reproducible, capped.

score = min(100, round(sum(weight * confidence))) with the model-sourced contribution capped at
15 points in total. No score ever leads to automated rejection; high/critical force review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

FRAMEWORK_VERSION = "fraud_v1"
LLM_CAP = Decimal("15")

Source = Literal["rule", "statistical", "llm"]
Severity = Literal["low", "medium", "high"]
Level = Literal["low", "medium", "high", "critical"]

WEIGHTS: dict[str, tuple[int, Severity, Source]] = {
    "F-DUP-CLAIM": (30, "high", "rule"),
    "F-DUP-DOC": (25, "high", "rule"),
    "F-DUP-INV": (25, "high", "rule"),
    "F-DATE-CONFLICT": (15, "medium", "rule"),
    "F-EARLY-CLAIM": (10, "medium", "rule"),
    "F-POST-EXPIRY-REPORT": (5, "low", "rule"),
    "F-ID-MISMATCH": (20, "high", "rule"),
    "F-FREQ": (10, "medium", "rule"),
    "F-AMOUNT-ANOM": (15, "medium", "statistical"),
    "F-ESTIMATE-OUTLIER": (10, "medium", "statistical"),
    "F-DUP-LINE": (10, "medium", "rule"),
    "F-INJECTION": (10, "medium", "rule"),
    "F-NARRATIVE": (15, "medium", "llm"),
}


@dataclass(frozen=True, slots=True)
class Indicator:
    rule_code: str
    description: str
    evidence: tuple[dict[str, object], ...]
    confidence: Decimal = Decimal("1")
    missing_evidence: tuple[str, ...] = ()

    @property
    def weight(self) -> int:
        return WEIGHTS[self.rule_code][0]

    @property
    def severity(self) -> Severity:
        return WEIGHTS[self.rule_code][1]

    @property
    def source(self) -> Source:
        return WEIGHTS[self.rule_code][2]


@dataclass(slots=True)
class Score:
    score: int
    level: Level
    recommended_action: Literal["continue", "review", "investigate"]
    contributions: dict[int, Decimal] = field(default_factory=dict)  # index -> points
    framework_version: str = FRAMEWORK_VERSION


def level_for(score: int) -> Level:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def score(
    indicators: list[Indicator], weights: dict[str, int] | None = None, llm_cap: Decimal = LLM_CAP
) -> Score:
    """``weights`` overrides framework defaults per rule code (from the product's rule set)."""
    if any(not i.evidence for i in indicators):
        raise ValueError("Every fraud indicator must cite evidence")
    contributions: dict[int, Decimal] = {}
    llm_budget = llm_cap
    for idx, ind in enumerate(indicators):
        weight = (weights or {}).get(ind.rule_code, ind.weight)
        points = Decimal(weight) * min(Decimal("1"), max(Decimal("0"), ind.confidence))
        if ind.source == "llm":
            points = min(points, llm_budget)
            llm_budget -= points
        contributions[idx] = points
    total = int(sum(contributions.values(), Decimal("0")).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    value = min(100, total)
    lvl = level_for(value)
    action: Literal["continue", "review", "investigate"] = (
        "continue" if lvl == "low" else "review" if lvl == "medium" else "investigate"
    )
    return Score(value, lvl, action, contributions)
