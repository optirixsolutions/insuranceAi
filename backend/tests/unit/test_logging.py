from __future__ import annotations

from app.core.logging import REDACTED, redact_processor, redact_value


def test_sensitive_keys_are_redacted() -> None:
    event = {"event": "login", "password": "hunter2", "Authorization": "Bearer abc", "user": "a@b.c"}
    out = redact_processor(None, "info", event)
    assert out["password"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["user"] == "a@b.c"


def test_nested_structures_are_redacted() -> None:
    value = {"outer": {"api_key": "k", "list": [{"session_token": "t"}, "plain"]}}
    assert redact_value(value) == {
        "outer": {"api_key": REDACTED, "list": [{"session_token": REDACTED}, "plain"]}
    }


def test_url_credentials_are_masked() -> None:
    masked = redact_value("connecting to postgresql+asyncpg://claimos_app:S3cret@db:5432/claimos")
    assert "S3cret" not in masked
    assert "claimos_app:[REDACTED]@db" in masked


def test_bearer_tokens_in_messages_are_masked() -> None:
    assert redact_value("header was Bearer eyJhbGciOi.abc-def") == "header was Bearer [REDACTED]"
