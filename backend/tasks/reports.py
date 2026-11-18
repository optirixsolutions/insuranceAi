"""Report generation and outbound messaging (``reports`` queue).

``reports.generate`` renders one queued report version. ``notifications.dispatch`` sends pending email
and SMS notifications and ``webhooks.dispatch`` delivers due webhooks; beat runs both every 10 s, so a
lost task or a failed send is retried without blocking the change that caused it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.services import notification_service, report_service, webhook_service
from tasks.celery_app import celery_app


async def _with_db(name: str, fn: Callable[[Database, Settings], Awaitable[Any]]) -> Any:
    settings = get_settings()
    db = Database.from_settings(settings, application_name=f"claimos-{name}")
    try:
        return await fn(db, settings)
    finally:
        await db.dispose()


@celery_app.task(name="reports.generate", time_limit=300, acks_late=True)
def generate(organization_id: str, report_id: str) -> str:
    status: str = asyncio.run(
        _with_db(
            "reports",
            lambda db, s: report_service.generate(db, s, uuid.UUID(organization_id), uuid.UUID(report_id)),
        )
    )
    return status


@celery_app.task(name="notifications.dispatch", time_limit=120)
def dispatch_notifications() -> dict[str, int]:
    result: dict[str, int] = asyncio.run(_with_db("notifications", notification_service.dispatch_pending))
    return result


@celery_app.task(name="webhooks.dispatch", time_limit=120)
def dispatch_webhooks() -> dict[str, int]:
    result: dict[str, int] = asyncio.run(_with_db("webhooks", webhook_service.dispatch_due))
    return result
