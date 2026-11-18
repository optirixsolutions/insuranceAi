"""Create required object-storage buckets if they do not exist.

Usage: python -m app.cli.storage_init
Run once per environment (compose runs it as the `storage-init` service).
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.integrations.storage import build_storage, required_buckets
from app.integrations.storage.base import StorageError


async def _main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json, service="storage-init")
    logger = get_logger("storage_init")
    storage = build_storage(settings)
    buckets = required_buckets(settings)
    for attempt in range(1, 31):
        try:
            for bucket in buckets:
                await storage.ensure_bucket(bucket)
            await storage.healthcheck(buckets)
        except StorageError as exc:
            logger.warning("storage_not_ready", attempt=attempt, error=str(exc))
            await asyncio.sleep(2)
            continue
        logger.info("storage_ready", provider=storage.provider_name, buckets=buckets)
        return 0
    logger.error("storage_init_failed", buckets=buckets)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
