"""Small helpers shared by domain services."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, or_

from app.core.errors import PreconditionFailedError


def like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def ilike_any(q: str, *columns: Any) -> ColumnElement[bool]:
    pattern = like_pattern(q)
    return or_(*(col.ilike(pattern) for col in columns))


def check_version(current: int, expected: int, what: str) -> None:
    if current != expected:
        raise PreconditionFailedError(f"This {what} was changed by someone else. Reload and try again.")


def changed_fields(obj: Any, changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply ``changes`` to ``obj`` and return (before, after) for the fields that actually changed."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for key, value in changes.items():
        old = getattr(obj, key)
        if old != value:
            before[key], after[key] = old, value
            setattr(obj, key, value)
    return before, after


def jsonable(values: dict[str, Any]) -> dict[str, Any]:
    """Make audit payloads JSON-safe (dates, decimals, UUIDs become strings)."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if value is None or isinstance(value, bool | int | str | list | dict):
            out[key] = value
        else:
            out[key] = str(value)
    return out
