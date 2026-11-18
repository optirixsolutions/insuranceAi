"""Application settings loaded from environment variables.

Production safety rules are enforced at startup: a misconfigured production
deployment fails fast instead of silently running with development adapters.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Markers used in .env.example placeholder secrets. Production refuses them.
_PLACEHOLDER_MARKERS = ("change_me", "change-me", "dev_only", "dev-only", "placeholder")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class DevelopmentAdapter(StrEnum):
    """Adapters that are only acceptable outside production. Surfaced in health output."""

    LOCAL_FILESYSTEM_STORAGE = "local_filesystem_storage"
    UNENCRYPTED_OBJECT_STORAGE = "unencrypted_object_storage"
    ANTIVIRUS_DISABLED = "antivirus_disabled"
    MOCK_AI_PROVIDER = "mock_ai_provider"
    DERIVED_ENCRYPTION_KEY = "derived_encryption_key"
    WEBHOOK_PRIVATE_HOSTS = "webhook_private_hosts"


def _secrets_dir() -> str | None:
    """Docker and Kubernetes secrets: one file per setting (``/run/secrets/database_url`` ...).

    ``SECRETS_DIR`` overrides the location. Environment variables still win over files, so a secret can
    be moved out of the environment one at a time.
    """
    configured = os.environ.get("SECRETS_DIR")
    if configured:
        return configured
    return "/run/secrets" if Path("/run/secrets").is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, secrets_dir=_secrets_dir())

    app_env: Environment = Environment.DEVELOPMENT
    app_name: str = "ClaimOS"
    app_version: str = "0.1.0"
    app_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8080")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    api_docs_enabled: bool | None = None

    # PostgreSQL. DATABASE_URL uses the least-privileged application role.
    database_url: SecretStr
    migration_database_url: SecretStr | None = None
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)

    redis_url: SecretStr
    celery_broker_url: SecretStr | None = None
    celery_result_backend: SecretStr | None = None

    storage_provider: Literal["s3", "local_filesystem"] = "s3"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_bucket_documents: str = "claimos-documents"
    s3_bucket_reports: str = "claimos-reports"
    s3_force_path_style: bool = True
    s3_server_side_encryption: Literal["AES256", "aws:kms", "none"] = "AES256"
    s3_kms_key_id: str | None = None
    local_storage_path: Path = Path("./.local-storage")

    health_check_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # Documents (docs/architecture/04-security-model.md §5)
    av_provider: Literal["clamav", "disabled_dev"] = "disabled_dev"
    clamav_host: str = "clamav"
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    max_documents_per_claim: int = Field(default=50, ge=1, le=500)
    max_pages_per_document: int = Field(default=200, ge=1, le=2000)
    ocr_engine: Literal["tesseract", "paddleocr"] = "tesseract"
    field_confidence_threshold: float = Field(default=0.85, ge=0.5, le=1.0)

    # Model gateway (docs/architecture/03-agent-contracts.md, ADR-010). The key never leaves the server.
    ai_provider: Literal["openrouter", "mock"] = "openrouter"
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    openrouter_default_model: str = "openrouter/free"
    openrouter_vision_model: str | None = None
    openrouter_reasoning_model: str | None = None
    openrouter_fast_model: str | None = None
    ai_request_timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    ai_max_transport_attempts: int = Field(default=3, ge=1, le=6)
    ai_daily_token_budget: int = Field(default=2_000_000, ge=1000)

    # Authentication and sessions (docs/architecture/04-security-model.md §2)
    session_idle_minutes: int = Field(default=30, ge=5, le=240)
    session_absolute_hours: int = Field(default=12, ge=1, le=24)
    session_cookie_secure: bool = True
    registration_enabled: bool | None = None
    login_max_account_failures: int = Field(default=5, ge=3, le=20)
    login_max_ip_failures: int = Field(default=30, ge=5, le=500)
    login_lockout_seconds: int = Field(default=900, ge=60, le=86400)
    access_token_ttl_hours: int = Field(default=72, ge=1, le=336)

    # Reports, notifications and webhooks (Phase 11)
    data_encryption_key: SecretStr | None = None  # 64 hex chars (AES-256); required in production
    email_provider: Literal["smtp", "disabled"] = "disabled"
    smtp_host: str = "mailpit"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_from: str = "ClaimOS <no-reply@claimos.test>"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    sms_provider: Literal["log", "disabled"] = "log"
    webhook_allowed_hosts: str = ""  # comma separated; may use http and private addresses (dev only)
    webhook_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    webhook_max_attempts: int = Field(default=6, ge=1, le=12)

    # Prometheus scrape token for /metrics (Phase 13). Unset: the endpoint is open on the internal network.
    metrics_token: SecretStr | None = None

    # Data retention (Phase 13): the nightly job deletes expired platform data older than these.
    # Claims, decisions, documents, reports and audit events are never deleted by it.
    retention_session_days: int = Field(default=30, ge=1, le=3650)
    retention_login_attempt_days: int = Field(default=90, ge=1, le=3650)
    retention_notification_days: int = Field(default=180, ge=1, le=3650)
    retention_webhook_delivery_days: int = Field(default=30, ge=1, le=3650)
    retention_checkpoint_days: int = Field(default=30, ge=1, le=3650)

    @property
    def webhook_private_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.webhook_allowed_hosts.split(",") if h.strip())

    @property
    def self_registration_allowed(self) -> bool:
        """Self-service organisation sign-up. Off in production unless explicitly enabled."""
        if self.registration_enabled is not None:
            return self.registration_enabled
        return not self.is_production

    @property
    def allowed_origin(self) -> str:
        url = self.app_base_url
        default_port = {"http": 80, "https": 443}.get(url.scheme)
        port = "" if url.port in (None, default_port) else f":{url.port}"
        return f"{url.scheme}://{url.host}{port}"

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return not self.is_production

    @property
    def broker_url(self) -> str:
        return (self.celery_broker_url or self.redis_url).get_secret_value()

    @property
    def result_backend_url(self) -> str:
        return (self.celery_result_backend or self.redis_url).get_secret_value()

    def development_adapters(self) -> list[DevelopmentAdapter]:
        adapters: list[DevelopmentAdapter] = []
        if self.storage_provider == "local_filesystem":
            adapters.append(DevelopmentAdapter.LOCAL_FILESYSTEM_STORAGE)
        elif self.s3_server_side_encryption == "none":
            adapters.append(DevelopmentAdapter.UNENCRYPTED_OBJECT_STORAGE)
        if self.av_provider == "disabled_dev":
            adapters.append(DevelopmentAdapter.ANTIVIRUS_DISABLED)
        if self.ai_provider == "mock":
            adapters.append(DevelopmentAdapter.MOCK_AI_PROVIDER)
        if self.data_encryption_key is None:
            adapters.append(DevelopmentAdapter.DERIVED_ENCRYPTION_KEY)
        if self.webhook_private_hosts:
            adapters.append(DevelopmentAdapter.WEBHOOK_PRIVATE_HOSTS)
        return adapters

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def _blank_key_is_missing(cls, value: object) -> object:
        """An empty ``OPENROUTER_API_KEY=`` line means "not configured", not an empty key."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        if self.storage_provider == "s3" and (
            self.s3_access_key_id is None or self.s3_secret_access_key is None
        ):
            raise ValueError("S3 storage requires S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY")
        key = self.data_encryption_key.get_secret_value() if self.data_encryption_key else None
        if key is not None and (len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key)):
            raise ValueError("DATA_ENCRYPTION_KEY must be 64 hexadecimal characters")
        if self.s3_server_side_encryption == "aws:kms" and not self.s3_kms_key_id:
            raise ValueError("S3_SERVER_SIDE_ENCRYPTION=aws:kms requires S3_KMS_KEY_ID")
        if self.is_production:
            self._validate_production()
        return self

    def _validate_production(self) -> None:
        problems: list[str] = []
        adapters = self.development_adapters()
        if adapters:
            problems.append(f"development adapters are not allowed in production: {adapters}")
        if self.app_base_url.scheme != "https":
            problems.append("APP_BASE_URL must use https in production")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true in production")
        secrets = {
            "DATABASE_URL": self.database_url,
            "REDIS_URL": self.redis_url,
            "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
            "OPENROUTER_API_KEY": self.openrouter_api_key,
            "DATA_ENCRYPTION_KEY": self.data_encryption_key,
        }
        for name, value in secrets.items():
            if value is not None and any(m in value.get_secret_value().lower() for m in _PLACEHOLDER_MARKERS):
                problems.append(f"{name} contains a placeholder value")
        if problems:
            raise ValueError("Invalid production configuration: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # required values come from the environment
