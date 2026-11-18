"""Celery application and queue topology.

Queues (see ADR-004):
  workflow       LangGraph orchestration (Phase 7)
  documents      OCR / document processing (Phase 4)
  reports        report generation and notifications (Phase 11)
  system         health pings and maintenance

Production runs a separate worker pool per queue so that no worker ever waits
on a task in its own queue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging
from kombu.entity import Queue

from app.core.config import get_settings
from app.core.logging import configure_logging

QUEUES = ("workflow", "documents", "reports", "system")


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery("claimos", broker=settings.broker_url, backend=settings.result_backend_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout": 3600},
        task_queues=[Queue(name) for name in QUEUES],
        task_default_queue="system",
        task_routes={
            "system.*": {"queue": "system"},
            "documents.*": {"queue": "documents"},
            "agents.*": {"queue": "workflow"},
            "workflow.*": {"queue": "workflow"},
            "reports.*": {"queue": "reports"},
            "notifications.*": {"queue": "reports"},
            "webhooks.*": {"queue": "reports"},
        },
        include=[
            "tasks.documents",
            "tasks.ai",
            "tasks.agents",
            "tasks.workflow",
            "tasks.reports",
            "tasks.maintenance",
        ],
        beat_schedule={
            "workflow-recover-stale": {"task": "workflow.recover_stale", "schedule": 15.0},
            "notifications-dispatch": {"task": "notifications.dispatch", "schedule": 10.0},
            "webhooks-dispatch": {"task": "webhooks.dispatch", "schedule": 10.0},
            # Nightly, outside working hours (UTC).
            "system-retention": {"task": "system.retention", "schedule": crontab(hour=2, minute=15)},
            "system-verify-audit-chains": {
                "task": "system.verify_audit_chains",
                "schedule": crontab(hour=2, minute=45),
            },
        },
        worker_hijack_root_logger=False,
    )
    return app


celery_app = create_celery_app()


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json, service="worker")


@celery_app.task(name="system.ping", bind=True)
def ping(self: Any) -> dict[str, str]:
    """Round-trip task used by readiness checks and smoke tests."""
    return {"worker": str(self.request.hostname), "at": datetime.now(UTC).isoformat()}
