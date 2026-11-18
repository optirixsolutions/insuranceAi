from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, StringConstraints


def _normalise_email(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


Email = Annotated[
    str,
    BeforeValidator(_normalise_email),
    StringConstraints(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Password = Annotated[str, StringConstraints(min_length=1, max_length=128)]
# PostgreSQL INET values arrive as ipaddress objects.
IpAddress = Annotated[str | None, BeforeValidator(lambda v: None if v is None else str(v))]


class Page[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int


class CursorPage[T](BaseModel):
    items: list[T]
    next_cursor: str | None
