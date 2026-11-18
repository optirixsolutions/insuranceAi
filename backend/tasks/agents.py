"""Agent run tasks (``workflow`` queue).

``agents.run`` executes one prepared run. ``agents.run_all`` runs the eight agents in pipeline
order on one claim, each as its own committed run: a convenience for exercising every agent on
real data before the Phase 7 LangGraph orchestrator (which adds checkpoints, branches, resume).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from redis.asyncio import Redis

from agents.registry import ORDER
from app.core.config import get_settings
from app.core.db import Database
from app.core.tenancy import bind_tenant
from app.services import agent_service
from app.services.audit_service import AuditActor
from model_gateway.throttle import Throttle
from tasks.celery_app import celery_app


async def _session(organization_id: str, fn: Any) -> Any:
    settings = get_settings()
    db = Database.from_settings(settings, application_name="claimos-agents")
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        async with db.sessionmaker() as session:
            await bind_tenant(session, uuid.UUID(organization_id))
            return await fn(session, settings, Throttle(redis), uuid.UUID(organization_id))
    finally:
        await redis.aclose()
        await db.dispose()


async def _run(organization_id: str, run_id: str) -> dict[str, Any]:
    async def go(session: Any, settings: Any, throttle: Throttle, org: uuid.UUID) -> dict[str, Any]:
        run = await agent_service.execute_run(session, settings, throttle, org, uuid.UUID(run_id))
        await session.commit()
        return {"run_id": str(run.id), "status": run.status}

    result: dict[str, Any] = await _session(organization_id, go)
    return result


async def _run_all(
    organization_id: str, claim_id: str, user_id: str | None, request_id: str | None
) -> dict[str, Any]:
    async def go(session: Any, settings: Any, throttle: Throttle, org: uuid.UUID) -> dict[str, Any]:
        statuses: dict[str, str] = {}
        uid = uuid.UUID(user_id) if user_id else None
        actor = AuditActor("user" if uid else "system", uid, request_id)
        for key in ORDER:
            run = await agent_service.create_run(
                session, org, uuid.UUID(claim_id), key, actor, request_id, uid
            )
            await session.commit()
            run = await agent_service.execute_run(session, settings, throttle, org, run.id)
            await session.commit()
            statuses[key] = run.status
        return {"claim_id": claim_id, "statuses": statuses}

    result: dict[str, Any] = await _session(organization_id, go)
    return result


@celery_app.task(name="agents.run", time_limit=1200)
def run(organization_id: str, run_id: str) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(_run(organization_id, run_id))
    return result


@celery_app.task(name="agents.run_all", time_limit=3600)
def run_all(
    organization_id: str, claim_id: str, user_id: str | None, request_id: str | None
) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(_run_all(organization_id, claim_id, user_id, request_id))
    return result
