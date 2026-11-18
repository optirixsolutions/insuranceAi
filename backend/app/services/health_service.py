"""Readiness checks across infrastructure dependencies.

Each check is an async callable returning an optional human-readable, non-sensitive
detail string, or raising on failure. Checks run concurrently with a timeout.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from app.core.logging import get_logger

logger = get_logger(__name__)

ComponentStatus = Literal["ok", "degraded", "down"]
CheckFn = Callable[[], Awaitable[str | None]]


class DegradedError(Exception):
    """Raised by a check that works but is not fully healthy (e.g. no workers yet)."""


class CheckFailedError(Exception):
    """Raised by a check whose failure message is safe to expose to clients."""


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    check: CheckFn
    required: bool = True


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    status: ComponentStatus
    required: bool
    latency_ms: float
    detail: str | None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: ComponentStatus
    components: list[ComponentHealth]


class HealthService:
    def __init__(self, checks: list[HealthCheck], *, timeout_seconds: float) -> None:
        self._checks = checks
        self._timeout = timeout_seconds

    async def _run(self, hc: HealthCheck) -> ComponentHealth:
        started = time.perf_counter()
        status: ComponentStatus
        try:
            detail = await asyncio.wait_for(hc.check(), timeout=self._timeout)
            status = "ok"
        except DegradedError as exc:
            status, detail = "degraded", str(exc)
        except CheckFailedError as exc:
            status, detail = "down", str(exc)
        except TimeoutError:
            status, detail = "down", f"timed out after {self._timeout:g}s"
            logger.warning("health_check_timeout", component=hc.name)
        except Exception as exc:
            # Only the exception type is exposed; the full error goes to logs.
            status, detail = "down", type(exc).__name__
            logger.warning("health_check_failed", component=hc.name, error=str(exc))
        latency = round((time.perf_counter() - started) * 1000, 1)
        return ComponentHealth(hc.name, status, hc.required, latency, detail)

    async def readiness(self) -> ReadinessReport:
        components = list(await asyncio.gather(*(self._run(hc) for hc in self._checks)))
        overall: ComponentStatus = "ok"
        for c in components:
            if c.status == "down" and c.required:
                overall = "down"
                break
            if c.status != "ok":
                overall = "degraded"
        return ReadinessReport(overall, components)
