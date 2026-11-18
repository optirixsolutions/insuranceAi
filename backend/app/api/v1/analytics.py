"""Dashboard and analytics (05 §2.11). Every endpoint is restricted to the caller's visible claims."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import DbDep, require
from app.security.permissions import P
from app.security.principal import Principal
from app.services import analytics_service, dashboard_service
from app.services.metrics_common import Scope

router = APIRouter(tags=["analytics"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Days = Annotated[int, Query(ge=1, le=365)]
Synthetic = Annotated[bool, Query()]


def _scope(principal: Principal, days: int, include_synthetic: bool) -> Scope:
    return Scope(principal, days, include_synthetic)


@router.get("/dashboard/summary")
async def dashboard_summary(
    principal: Reader, db: DbDep, days: Days = 30, include_synthetic: Synthetic = True
) -> dict[str, Any]:
    return await dashboard_service.summary(db, _scope(principal, days, include_synthetic))


@router.get("/dashboard/activity")
async def dashboard_activity(
    principal: Reader, db: DbDep, include_synthetic: Synthetic = True
) -> dict[str, Any]:
    return await dashboard_service.activity(db, _scope(principal, 1, include_synthetic))


@router.get("/analytics/processing-times")
async def processing_times(
    principal: Reader,
    db: DbDep,
    days: Days = 30,
    include_synthetic: Synthetic = True,
    product_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    scope = _scope(principal, days, include_synthetic)
    return await analytics_service.processing_times(db, scope, product_id)


@router.get("/analytics/claims")
async def claims_analytics(
    principal: Reader,
    db: DbDep,
    days: Days = 30,
    include_synthetic: Synthetic = True,
    bucket: Literal["day", "week"] = "day",
) -> dict[str, Any]:
    return await analytics_service.claims(db, _scope(principal, days, include_synthetic), bucket)


@router.get("/analytics/fraud")
async def fraud_analytics(
    principal: Reader, db: DbDep, days: Days = 30, include_synthetic: Synthetic = True
) -> dict[str, Any]:
    return await analytics_service.fraud(db, _scope(principal, days, include_synthetic))


@router.get("/analytics/agents")
async def agent_analytics(
    principal: Reader, db: DbDep, days: Days = 30, include_synthetic: Synthetic = True
) -> dict[str, Any]:
    return await analytics_service.agents(db, _scope(principal, days, include_synthetic))


@router.get("/analytics/extraction-accuracy")
async def extraction_accuracy(
    principal: Reader, db: DbDep, days: Days = 30, include_synthetic: Synthetic = True
) -> dict[str, Any]:
    return await analytics_service.extraction_accuracy(db, _scope(principal, days, include_synthetic))


@router.get("/assessments")
async def assessments(
    principal: Reader,
    db: DbDep,
    include_synthetic: Synthetic = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    return await analytics_service.assessments(db, _scope(principal, 365, include_synthetic), limit)
