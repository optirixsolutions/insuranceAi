"""Shared building blocks for dashboard and analytics metrics (Phase 10).

Every metric is a single SQL aggregate restricted to ``Scope.claims()``: the caller's tenant, the
claims they may read (assigned-only users see only their claims), not deleted, and synthetic claims
only when asked. Nothing is estimated: a metric with no underlying rows is ``None``, never 0%.
Each metric carries the plain-language definition of its query, shown in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, func, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.claims import Claim
from app.security.principal import Principal
from app.services.claim_access import scope_claims, sees_all_claims

Unit = Literal["count", "ratio", "ms", "usd"]


@dataclass(frozen=True, slots=True)
class Scope:
    principal: Principal
    days: int
    include_synthetic: bool

    @property
    def since(self) -> datetime:
        return datetime.now(UTC) - timedelta(days=self.days)

    @property
    def org_wide(self) -> bool:
        return sees_all_claims(self.principal)

    def claims(self) -> Select[Any]:
        """The claim ids every metric is restricted to."""
        stmt: Select[Any] = scope_claims(select(Claim.id), self.principal)
        if not self.include_synthetic:
            stmt = stmt.where(Claim.is_synthetic.is_(False))
        return stmt

    def visible(self, claim_id: Any) -> ColumnElement[bool]:
        result: ColumnElement[bool] = claim_id.in_(self.claims().scalar_subquery())
        return result


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    label: str
    value: int | float | Decimal | None
    unit: Unit
    definition: str

    def as_dict(self) -> dict[str, Any]:
        value = float(self.value) if isinstance(self.value, Decimal) else self.value
        return {
            "key": self.key,
            "label": self.label,
            "value": value,
            "unit": self.unit,
            "definition": self.definition,
        }


def ratio(numerator: int | None, denominator: int | None) -> float | None:
    """A rate over zero rows is unknown, not 0%."""
    if not denominator:
        return None
    return round((numerator or 0) / denominator, 4)


def percentile(fraction: float, column: Any) -> Any:
    return func.percentile_cont(fraction).within_group(column)


def ms(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)
