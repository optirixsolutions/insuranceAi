"""Summary statistics for benchmark samples.

Percentiles use linear interpolation between closest ranks, the same definition as PostgreSQL's
``percentile_cont`` used by the dashboard (``app.services.metrics_common.percentile``), so figures from the
benchmark and from the processing-time analytics can be compared directly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summary(values: Sequence[float]) -> dict[str, Any]:
    """n, min, P50, P90, P95, P99, max and mean (None when there are no samples)."""
    if not values:
        return {
            "n": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "n": len(values),
        "min": min(values),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def meets(values: Sequence[float], fraction: float, limit: float) -> bool | None:
    """Whether the given percentile is at or under the limit; None without samples."""
    value = percentile(values, fraction)
    return None if value is None else value <= limit
