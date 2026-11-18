"""Versioned rule-set configuration (Phase 8). One published version per product applies at a time.

Everything a product owner may tune lives here and is validated strictly; code keeps the order of
operations (03 §5), so a configuration can change numbers but never the shape of a calculation.
``DEFAULT_CONFIG`` reproduces the Phase 6 constants exactly, so behaviour is unchanged until an
organisation publishes its own version.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rules_engine.fraud_scoring import WEIGHTS

Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
Amount = Annotated[Decimal, Field(ge=0, max_digits=19, decimal_places=4)]
HEALTH_CATEGORIES = frozenset({"room", "consultation", "procedure", "medication", "diagnostics", "other"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DepreciationBand(Strict):
    min_age_years: Annotated[int, Field(ge=0, le=50)]
    pct: Pct


class StpConfig(Strict):
    enabled: bool = False
    max_amount: Amount | None = None  # required when enabled
    max_fraud_score: Annotated[int, Field(ge=0, le=100)] = 25
    claimant_flag_months: Annotated[int, Field(ge=0, le=60)] = 12


class RuleSetConfig(Strict):
    motor_parts_depreciation: list[DepreciationBand]
    health_sub_limits: dict[str, Amount] = {}  # per category; a policy endorsement overrides
    fraud_weights: dict[str, Annotated[int, Field(ge=0, le=100)]] = {}  # overrides of framework defaults
    fraud_llm_cap: Annotated[int, Field(ge=0, le=30)] = 15
    validation_amount_tolerance_pct: Pct = Decimal("10")
    automated_approval_allowed: bool = False  # compliance profile
    stp: StpConfig = StpConfig()
    # Human review (Phase 9): approvals above this need a second approver (None = only overrides and
    # high fraud risk do); a review is due this many hours after it is opened.
    four_eyes_threshold: Amount | None = Decimal("5000")
    review_sla_hours: Annotated[int, Field(ge=1, le=720)] = 48

    @model_validator(mode="after")
    def _check(self) -> Self:
        ages = [b.min_age_years for b in self.motor_parts_depreciation]
        if 0 not in ages:
            raise ValueError("motor_parts_depreciation needs a band starting at age 0")
        if len(set(ages)) != len(ages):
            raise ValueError("motor_parts_depreciation has duplicate ages")
        by_age = sorted(self.motor_parts_depreciation, key=lambda b: b.min_age_years)
        if any(later.pct < earlier.pct for earlier, later in pairwise(by_age)):
            raise ValueError("depreciation must not decrease as vehicles get older")
        unknown = set(self.fraud_weights) - WEIGHTS.keys()
        if unknown:
            raise ValueError(f"unknown fraud indicators: {sorted(unknown)}")
        if set(self.health_sub_limits) - HEALTH_CATEGORIES:
            raise ValueError(f"health sub-limit categories must be among {sorted(HEALTH_CATEGORIES)}")
        if self.stp.enabled and not self.automated_approval_allowed:
            raise ValueError("STP needs the compliance profile to allow automated approval")
        if self.stp.enabled and self.stp.max_amount is None:
            raise ValueError("STP needs a maximum amount")
        return self

    def depreciation_table(self) -> tuple[tuple[int, Decimal], ...]:
        """Bands as (floor, pct), highest floor first (the calculator takes the first that applies)."""
        bands = sorted(self.motor_parts_depreciation, key=lambda b: -b.min_age_years)
        return tuple((b.min_age_years, b.pct) for b in bands)

    def weight(self, code: str) -> int:
        return self.fraud_weights.get(code, WEIGHTS[code][0])


DEFAULT_CONFIG = RuleSetConfig(
    motor_parts_depreciation=[
        DepreciationBand(min_age_years=a, pct=Decimal(p))
        for a, p in ((0, "0"), (1, "5"), (2, "10"), (3, "15"), (5, "25"), (7, "35"), (10, "50"))
    ]
)
DEFAULT_VERSION = "rules_v1-default"
