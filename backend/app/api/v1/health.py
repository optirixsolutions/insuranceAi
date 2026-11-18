from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.core.config import Settings
from app.schemas.health import ComponentHealthOut, LivenessResponse, ReadinessResponse
from app.services.health_service import HealthService

router = APIRouter(prefix="/health", tags=["health"])


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_health_service(request: Request) -> HealthService:
    service: HealthService = request.app.state.health_service
    return service


@router.get("/live", response_model=LivenessResponse)
async def live(settings: Annotated[Settings, Depends(get_settings_dep)]) -> LivenessResponse:
    """Process liveness. Never touches dependencies."""
    return LivenessResponse(status="ok", version=settings.app_version)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A required dependency is down"}},
)
async def ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    health: Annotated[HealthService, Depends(get_health_service)],
) -> ReadinessResponse:
    """Dependency readiness. Returns 503 when any required component is down."""
    report = await health.readiness()
    if report.status == "down":
        response.status_code = 503
    return ReadinessResponse(
        status=report.status,
        version=settings.app_version,
        environment=settings.app_env.value,
        checked_at=datetime.now(UTC),
        components=[
            ComponentHealthOut(
                name=c.name, status=c.status, required=c.required, latency_ms=c.latency_ms, detail=c.detail
            )
            for c in report.components
        ],
        development_adapters=[a.value for a in settings.development_adapters()],
    )
