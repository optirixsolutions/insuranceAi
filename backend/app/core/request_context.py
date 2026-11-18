"""Per-request context (request id) shared by logging, errors and audit."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


def resolve_request_id(incoming: str | None) -> str:
    """Accept a well-formed client/proxy request id, otherwise generate one."""
    if incoming and _VALID_REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex
