from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["ok", "degraded", "down"]


class LivenessResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ComponentHealthOut(BaseModel):
    name: str
    status: Status
    required: bool
    latency_ms: float
    detail: str | None


class ReadinessResponse(BaseModel):
    status: Status
    version: str
    environment: str
    checked_at: datetime
    components: list[ComponentHealthOut]
    development_adapters: list[str]
