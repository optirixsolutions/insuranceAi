"""Nightly maintenance on the ``system`` queue (Phase 13): data retention and audit-chain verification."""

from __future__ import annotations

import asyncio
from typing import Any

from app.services import maintenance_service
from tasks.celery_app import celery_app
from tasks.reports import _with_db


@celery_app.task(name="system.retention", time_limit=900)
def retention() -> dict[str, int]:
    result: dict[str, int] = asyncio.run(_with_db("retention", maintenance_service.run_retention))
    return result


@celery_app.task(name="system.verify_audit_chains", time_limit=1800)
def verify_audit_chains() -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(_with_db("audit-verify", maintenance_service.verify_audit_chains))
    return result
