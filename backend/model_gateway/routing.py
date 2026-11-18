"""Choose which models may serve a request (ADR-010).

A model is eligible only if it is active, has *verified* (probed) every capability the agent
requires, and is allowed to see the request's data: real (non-synthetic) claim data may only go
to models whose data policy an administrator approved, in organisations that approved AI
processing of real data. Unverified catalogue claims are never enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DataClass = Literal["synthetic", "real"]
REAL_DATA_POLICIES = frozenset({"no_training", "zero_retention"})


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: str
    slug: str
    status: str
    data_policy: str
    verified: frozenset[str]
    requests_per_minute: int
    prompt_price_per_mtok: float = 0.0
    completion_price_per_mtok: float = 0.0


@dataclass(frozen=True, slots=True)
class Exclusion:
    slug: str
    reason: str


# A verified capability that strictly subsumes another satisfies it: a model that returns valid
# output under a strict JSON schema is used with that schema, which is stronger than JSON mode.
IMPLIES: dict[str, frozenset[str]] = {"json_schema": frozenset({"json_mode"})}


def effective(verified: frozenset[str]) -> frozenset[str]:
    return verified.union(*(IMPLIES.get(c, frozenset()) for c in verified))


def eligibility(
    model: ModelProfile, *, required: set[str], data_class: DataClass, org_allows_real_data: bool
) -> str | None:
    """None if the model may be used, else the reason it may not."""
    if model.status != "active":
        return "disabled"
    missing = required - effective(model.verified)
    if missing:
        return f"capabilities not verified: {', '.join(sorted(missing))}"
    if data_class == "real":
        if not org_allows_real_data:
            return "organisation has not approved AI processing of real data"
        if model.data_policy not in REAL_DATA_POLICIES:
            return f"data policy '{model.data_policy}' not approved for real data"
    return None


def candidates(
    chain: list[ModelProfile], *, required: set[str], data_class: DataClass, org_allows_real_data: bool
) -> tuple[list[ModelProfile], list[Exclusion]]:
    """Primary first, then fallbacks, minus anything ineligible (with the reason recorded)."""
    usable: list[ModelProfile] = []
    excluded: list[Exclusion] = []
    seen: set[str] = set()
    for model in chain:
        if model.slug in seen:
            continue
        seen.add(model.slug)
        reason = eligibility(
            model, required=required, data_class=data_class, org_allows_real_data=org_allows_real_data
        )
        (excluded.append(Exclusion(model.slug, reason)) if reason else usable.append(model))
    return usable, excluded
