"""Claim status state machine (docs/architecture/02-workflow.md §1).

Transitions are tagged with who may perform them:
* ``workflow``: only the orchestration engine (Phase 7) — e.g. moving between processing stages.
* ``human``:    a person through the API, with a reason.
* ``decision``: only together with a recorded claim decision (Phase 9). Never available as a
                plain status change, so APPROVED/REJECTED cannot be reached without one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

Actor = Literal["workflow", "human", "decision"]


class ClaimStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    DOCUMENT_PROCESSING = "DOCUMENT_PROCESSING"
    DOCUMENT_REVIEW = "DOCUMENT_REVIEW"
    POLICY_VERIFICATION = "POLICY_VERIFICATION"
    FRAUD_ANALYSIS = "FRAUD_ANALYSIS"
    CLAIM_ASSESSMENT = "CLAIM_ASSESSMENT"
    VALIDATION = "VALIDATION"
    DECISION_PENDING = "DECISION_PENDING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    AWAITING_INFORMATION = "AWAITING_INFORMATION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


S = ClaimStatus
TERMINAL = frozenset({S.COMPLETED})

_WORKFLOW_ONLY: frozenset[Actor] = frozenset({"workflow"})
_WITH_HUMAN: frozenset[Actor] = frozenset({"workflow", "human"})

_TRANSITIONS: dict[tuple[ClaimStatus, ClaimStatus], frozenset[Actor]] = {
    (S.SUBMITTED, S.DOCUMENT_PROCESSING): frozenset({"workflow"}),
    (S.SUBMITTED, S.AWAITING_INFORMATION): frozenset({"workflow", "human"}),
    (S.DOCUMENT_PROCESSING, S.DOCUMENT_REVIEW): frozenset({"workflow"}),
    (S.DOCUMENT_PROCESSING, S.POLICY_VERIFICATION): frozenset({"workflow"}),
    (S.DOCUMENT_PROCESSING, S.FAILED): frozenset({"workflow"}),
    (S.DOCUMENT_REVIEW, S.DOCUMENT_PROCESSING): frozenset({"workflow", "human"}),
    (S.POLICY_VERIFICATION, S.FRAUD_ANALYSIS): frozenset({"workflow"}),
    (S.POLICY_VERIFICATION, S.FAILED): frozenset({"workflow"}),
    (S.FRAUD_ANALYSIS, S.CLAIM_ASSESSMENT): frozenset({"workflow"}),
    (S.CLAIM_ASSESSMENT, S.VALIDATION): frozenset({"workflow"}),
    (S.CLAIM_ASSESSMENT, S.FAILED): frozenset({"workflow"}),
    (S.VALIDATION, S.DECISION_PENDING): frozenset({"workflow"}),
    (S.DECISION_PENDING, S.HUMAN_REVIEW): frozenset({"workflow"}),
    (S.DECISION_PENDING, S.AWAITING_INFORMATION): frozenset({"workflow"}),
    (S.DECISION_PENDING, S.APPROVED): frozenset({"decision"}),
    (S.HUMAN_REVIEW, S.APPROVED): frozenset({"decision"}),
    (S.HUMAN_REVIEW, S.REJECTED): frozenset({"decision"}),
    (S.HUMAN_REVIEW, S.AWAITING_INFORMATION): frozenset({"decision"}),
    (S.HUMAN_REVIEW, S.CLAIM_ASSESSMENT): frozenset({"decision"}),
    (S.HUMAN_REVIEW, S.HUMAN_REVIEW): frozenset({"decision"}),
    (S.AWAITING_INFORMATION, S.SUBMITTED): frozenset({"human"}),
    (S.AWAITING_INFORMATION, S.DOCUMENT_PROCESSING): frozenset({"workflow"}),
    (S.APPROVED, S.COMPLETED): frozenset({"workflow"}),
    (S.REJECTED, S.COMPLETED): frozenset({"workflow"}),
    (S.FAILED, S.DOCUMENT_PROCESSING): frozenset({"workflow", "human"}),
    # Phase 7 orchestration: intake can stop for missing information before document processing,
    # any processing stage can fail, and a cancelled execution returns the claim to SUBMITTED.
    (S.DOCUMENT_PROCESSING, S.AWAITING_INFORMATION): frozenset({"workflow"}),
    (S.FRAUD_ANALYSIS, S.FAILED): frozenset({"workflow"}),
    (S.VALIDATION, S.FAILED): frozenset({"workflow"}),
    (S.DECISION_PENDING, S.FAILED): frozenset({"workflow"}),
    (S.DOCUMENT_REVIEW, S.FAILED): frozenset({"workflow"}),
    (S.HUMAN_REVIEW, S.FAILED): frozenset({"workflow"}),
    (S.AWAITING_INFORMATION, S.HUMAN_REVIEW): frozenset({"workflow"}),  # more-info limit escalation
    **{
        (stage, S.SUBMITTED): _WITH_HUMAN if stage is S.AWAITING_INFORMATION else _WORKFLOW_ONLY
        for stage in (
            S.DOCUMENT_PROCESSING,
            S.DOCUMENT_REVIEW,
            S.POLICY_VERIFICATION,
            S.FRAUD_ANALYSIS,
            S.CLAIM_ASSESSMENT,
            S.VALIDATION,
            S.DECISION_PENDING,
            S.AWAITING_INFORMATION,
            S.FAILED,
        )
    },
}


class InvalidTransitionError(ValueError):
    def __init__(self, current: ClaimStatus, target: ClaimStatus, actor: Actor) -> None:
        super().__init__(f"A claim cannot move from {current} to {target} by a {actor} action")
        self.current, self.target, self.actor = current, target, actor


def can_transition(current: ClaimStatus, target: ClaimStatus, actor: Actor) -> bool:
    return actor in _TRANSITIONS.get((current, target), frozenset())


def assert_transition(current: ClaimStatus, target: ClaimStatus, actor: Actor) -> None:
    if not can_transition(current, target, actor):
        raise InvalidTransitionError(current, target, actor)


def allowed_targets(current: ClaimStatus, actor: Actor) -> list[ClaimStatus]:
    return sorted(t for (c, t), actors in _TRANSITIONS.items() if c == current and actor in actors)


def path(current: ClaimStatus, target: ClaimStatus, actor: Actor) -> list[ClaimStatus]:
    """Shortest sequence of allowed transitions from ``current`` to ``target`` (excluding ``current``).

    The workflow moves the claim stage by stage so the status history stays truthful even when
    parallel branches finish out of order. Raises if the target is unreachable for this actor.
    """
    if current == target:
        return []
    frontier: list[list[ClaimStatus]] = [[current]]
    seen = {current}
    while frontier:
        route = frontier.pop(0)
        for (c, t), actors in _TRANSITIONS.items():
            if c != route[-1] or actor not in actors or t in seen:
                continue
            if t == target:
                return [*route[1:], t]
            seen.add(t)
            frontier.append([*route, t])
    raise InvalidTransitionError(current, target, actor)
