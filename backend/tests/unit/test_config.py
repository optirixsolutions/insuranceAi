from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DevelopmentAdapter, Settings

S3 = {
    "storage_provider": "s3",
    "s3_access_key_id": "AKIAEXAMPLE",
    "s3_secret_access_key": "s3cr3t-value",
}
PROD = {
    "app_env": "production",
    "app_base_url": "https://claims.example.com",
    "database_url": "postgresql+asyncpg://claimos_app:Str0ngGenerated@db:5432/claimos",
    "redis_url": "redis://:Str0ngGenerated@redis:6379/0",
    "av_provider": "clamav",
    "data_encryption_key": "ab" * 32,
    **S3,
}
KEY = {"data_encryption_key": "cd" * 32}
FAKE_KEY = "not-a-real-key"  # test value, never sent anywhere


def test_requires_asyncpg_driver(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        make_settings(database_url="postgresql://u:p@h/db")


def test_s3_requires_credentials(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="S3_ACCESS_KEY_ID"):
        make_settings(storage_provider="s3")


def test_kms_requires_key_id(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="S3_KMS_KEY_ID"):
        make_settings(**S3, s3_server_side_encryption="aws:kms")


def test_valid_production_configuration(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(**PROD)
    assert settings.is_production
    assert settings.development_adapters() == []
    assert settings.docs_enabled is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"storage_provider": "local_filesystem"}, "development adapters"),
        ({"s3_server_side_encryption": "none"}, "development adapters"),
        ({"av_provider": "disabled_dev"}, "development adapters"),
        ({"data_encryption_key": None}, "derived_encryption_key"),
        ({"webhook_allowed_hosts": "receiver.internal"}, "webhook_private_hosts"),
        ({"data_encryption_key": "abc"}, "64 hexadecimal"),
        ({"app_base_url": "http://claims.example.com"}, "https"),
        ({"s3_secret_access_key": "change_me"}, "S3_SECRET_ACCESS_KEY contains a placeholder"),
        (
            {"database_url": "postgresql+asyncpg://claimos_app:dev_only_pw@db/claimos"},
            "DATABASE_URL contains a placeholder",
        ),
    ],
)
def test_production_rejects_unsafe_configuration(
    make_settings: Callable[..., Settings], overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**{**PROD, **overrides})


def test_encryption_key_must_be_32_bytes(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="64 hexadecimal"):
        make_settings(data_encryption_key="ab" * 16)
    assert make_settings(**KEY).data_encryption_key is not None


def test_development_adapters_reported(make_settings: Callable[..., Settings]) -> None:
    assert make_settings().development_adapters() == [
        DevelopmentAdapter.LOCAL_FILESYSTEM_STORAGE,
        DevelopmentAdapter.ANTIVIRUS_DISABLED,
        DevelopmentAdapter.DERIVED_ENCRYPTION_KEY,
    ]
    assert make_settings(
        **S3, **KEY, s3_server_side_encryption="none", av_provider="clamav"
    ).development_adapters() == [DevelopmentAdapter.UNENCRYPTED_OBJECT_STORAGE]
    assert make_settings(**S3, **KEY, av_provider="clamav").development_adapters() == []
    assert make_settings(
        **S3, **KEY, av_provider="clamav", webhook_allowed_hosts="a, b"
    ).development_adapters() == [DevelopmentAdapter.WEBHOOK_PRIVATE_HOSTS]


def test_blank_openrouter_key_means_not_configured(make_settings: Callable[..., Settings]) -> None:
    assert make_settings(openrouter_api_key="   ").openrouter_api_key is None
    assert make_settings(openrouter_api_key=FAKE_KEY).openrouter_api_key is not None


def test_broker_defaults_to_redis(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(redis_url="redis://cache:6379/3")
    assert settings.broker_url == "redis://cache:6379/3"
    assert settings.result_backend_url == "redis://cache:6379/3"


def test_docs_can_be_explicitly_enabled_in_production(make_settings: Callable[..., Settings]) -> None:
    assert make_settings(**PROD, api_docs_enabled=True).docs_enabled is True


def test_secrets_are_not_rendered(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(**S3)
    assert "s3cr3t-value" not in repr(settings)


def test_secrets_are_read_from_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_settings: Callable[..., Settings]
) -> None:
    """Docker/Kubernetes secrets: one file per setting; an environment variable still wins."""
    base = make_settings()
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK_TIMEOUT_SECONDS", "7")
    (tmp_path / "smtp_password").write_text("from-a-file\n")
    (tmp_path / "openrouter_api_key").write_text("sk-or-file")
    (tmp_path / "webhook_timeout_seconds").write_text("20")
    loaded = Settings(
        _secrets_dir=str(tmp_path),
        database_url=base.database_url.get_secret_value(),
        redis_url=base.redis_url.get_secret_value(),
    )
    assert loaded.smtp_password is not None and loaded.smtp_password.get_secret_value() == "from-a-file"
    assert loaded.openrouter_api_key is not None
    assert loaded.openrouter_api_key.get_secret_value() == "sk-or-file"
    assert loaded.webhook_timeout_seconds == 7.0
