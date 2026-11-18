"""Shared graph state (docs/architecture/02-workflow.md §4).

The state holds identifiers and compact, code-generated summaries only; full results live in
``agent_runs`` / ``agent_outputs``. Document text never enters the state. Dict fields merge so the
parallel policy and fraud branches can both write.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    return {**(left or {}), **(right or {})}


class ClaimState(TypedDict, total=False):
    organization_id: str
    claim_id: str
    execution_id: str
    runs: Annotated[dict[str, str], merge]  # agent key -> latest agent_run id
    summaries: Annotated[dict[str, Any], merge]  # node -> compact summary for UI/interrupts
    completed: Annotated[list[str], operator.add]  # node visit log
    errors: Annotated[list[dict[str, Any]], operator.add]
    route: str  # last routing decision taken by a router node
    recheck_intake: bool  # intake ran while uploads were still processing
    review_override: bool  # a person chose to continue despite unverified fields
    reassessment_count: int
    more_info_count: int
    review_round: int  # HUMAN_REVIEW visits that returned (keys the review row per interrupt)
    resume: dict[str, Any]  # payload of the last resume
