"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.metrics import metrics_endpoint, set_app_info
from app.core.migrations import expected_heads
from app.integrations.antivirus import ClamAVScanner
from app.integrations.storage import build_storage, required_buckets
from app.middleware.origin import OriginCheckMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.services.health_checks import (
    antivirus_check,
    database_check,
    migrations_check,
    model_gateway_check,
    redis_check,
    storage_check,
    workers_check,
)
from app.services.health_service import HealthCheck, HealthService

logger = get_logger(__name__)


def _build_health_service(settings: Settings, db: Database, redis: Redis) -> HealthService:
    from tasks.celery_app import celery_app  # imported lazily: only the API needs the client

    storage = build_storage(settings)
    timeout = settings.health_check_timeout_seconds
    return HealthService(
        [
            HealthCheck("database", database_check(db)),
            HealthCheck("migrations", migrations_check(db, expected_heads())),
            HealthCheck("redis", redis_check(redis)),
            HealthCheck("object_storage", storage_check(storage, required_buckets(settings))),
            HealthCheck("workers", workers_check(celery_app, timeout_seconds=timeout / 2), required=False),
            HealthCheck("model_gateway", model_gateway_check(settings), required=False),
            *(
                [
                    HealthCheck(
                        "antivirus",
                        antivirus_check(
                            ClamAVScanner(settings.clamav_host, settings.clamav_port, timeout_seconds=timeout)
                        ),
                        required=False,
                    )
                ]
                if settings.av_provider == "clamav"
                else []
            ),
        ],
        timeout_seconds=timeout,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json, service="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database.from_settings(settings)
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=settings.health_check_timeout_seconds,
            socket_timeout=settings.health_check_timeout_seconds,
            health_check_interval=30,
        )
        app.state.db = db
        app.state.redis = redis
        if not hasattr(app.state, "health_service"):
            app.state.health_service = _build_health_service(settings, db, redis)
        adapters = [a.value for a in settings.development_adapters()]
        logger.info("startup", environment=settings.app_env.value, version=settings.app_version)
        if adapters:
            logger.warning("development_adapters_active", adapters=adapters)
        try:
            yield
        finally:
            await redis.aclose()
            await db.dispose()
            logger.info("shutdown")

    app = FastAPI(
        title="ClaimOS API",
        version=settings.app_version,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url="/api/redoc" if settings.docs_enabled else None,
        openapi_url="/api/v1/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    register_exception_handlers(app)
    app.include_router(api_router)
    # Scraped on the API's own port; Nginx only proxies /api/, so this is never public.
    app.add_route("/metrics", metrics_endpoint(settings), methods=["GET"], include_in_schema=False)
    set_app_info(settings)
    # Order: outermost added last. Request id must wrap everything so errors carry it.
    app.add_middleware(OriginCheckMiddleware, allowed_origin=settings.allowed_origin)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.add_middleware(RequestIdMiddleware)
    return app
