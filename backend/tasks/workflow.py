"""Workflow tasks (``workflow`` queue) and the crash-recovery beat task.

``workflow.run`` executes (or resumes) one execution until it completes, interrupts, fails or is
cancelled; an interrupt ends the task, so no worker is held while a person decides. The task id is
the execution's ``celery_task_id``; any other delivery of the same execution is skipped.
``workflow.recover_stale`` (every 15 s via beat) re-enqueues executions whose worker stopped
heartbeating; LangGraph then replays from the last checkpoint.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from app.core.config import get_settings
from app.services import workflow_service
from tasks.celery_app import celery_app

STALE_SECONDS = int(os.environ.get("WORKFLOW_STALE_SECONDS", "60"))


@celery_app.task(name="workflow.run", bind=True, time_limit=3600, acks_late=True)
def run(self: Any, organization_id: str, execution_id: str, resume: dict[str, Any] | None = None) -> str:
    outcome: str = asyncio.run(
        workflow_service.run(
            get_settings(), uuid.UUID(organization_id), uuid.UUID(execution_id), self.request.id, resume
        )
    )
    return outcome


@celery_app.task(name="workflow.recover_stale", time_limit=120)
def recover_stale() -> list[str]:
    recovered: list[str] = asyncio.run(workflow_service.recover_stale(get_settings(), STALE_SECONDS))
    return recovered
