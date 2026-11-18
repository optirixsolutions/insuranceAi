"""Structured logging with secret/PII redaction.

All application and library logs (uvicorn, celery, sqlalchemy) are routed through
structlog so every line is JSON in containers and carries the request id.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "session",
    "csrf",
    "national_id",
    "bank_account",
    "iban",
)

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]+")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_value(value: Any) -> Any:
    """Recursively redact sensitive mapping keys and credentials embedded in strings."""
    if isinstance(value, str):
        value = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:" + REDACTED + "@", value)
        return _BEARER.sub("Bearer " + REDACTED, value)
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_sensitive_key(str(k)) else redact_value(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


def redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if _is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact_value(event_dict[key])
    return event_dict


def configure_logging(*, level: str = "INFO", json_logs: bool = True, service: str = "api") -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_processor,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    structlog.contextvars.bind_contextvars(service=service)

    # Route framework loggers through the root handler; access logs come from our middleware.
    for name in ("uvicorn", "uvicorn.error", "celery", "sqlalchemy.engine"):
        lib = logging.getLogger(name)
        lib.handlers = []
        lib.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
