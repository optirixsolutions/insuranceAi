"""Standard error model (RFC 9457 problem+json) and exception handlers."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger
from app.core.request_context import get_request_id

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://claimos.dev/problems/"

logger = get_logger(__name__)


class FieldError(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class Problem(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    request_id: str | None = None
    errors: list[FieldError] | None = None


class AppError(Exception):
    """Base class for errors that are safe to show to API clients."""

    status_code: ClassVar[int] = HTTPStatus.INTERNAL_SERVER_ERROR
    code: ClassVar[str] = "INTERNAL"
    title: ClassVar[str] = "Internal server error"

    def __init__(self, detail: str | None = None, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.headers = headers


class UnauthenticatedError(AppError):
    status_code = HTTPStatus.UNAUTHORIZED
    code = "UNAUTHENTICATED"
    title = "Authentication required"


class ForbiddenError(AppError):
    status_code = HTTPStatus.FORBIDDEN
    code = "FORBIDDEN"
    title = "Permission denied"


class AuthorityExceededError(AppError):
    """A reviewer acted outside their decision authority (amount, product, reject, fraud override,
    segregation of duties)."""

    status_code = HTTPStatus.FORBIDDEN
    code = "AUTHORITY_EXCEEDED"
    title = "Outside your decision authority"


class NotFoundError(AppError):
    status_code = HTTPStatus.NOT_FOUND
    code = "NOT_FOUND"
    title = "Resource not found"


class ConflictError(AppError):
    status_code = HTTPStatus.CONFLICT
    code = "CONFLICT"
    title = "Conflict"


class PreconditionFailedError(AppError):
    status_code = HTTPStatus.PRECONDITION_FAILED
    code = "PRECONDITION_FAILED"
    title = "The resource was modified by someone else"


class PreconditionRequiredError(AppError):
    status_code = HTTPStatus.PRECONDITION_REQUIRED
    code = "PRECONDITION_REQUIRED"
    title = "If-Match header is required"


class RateLimitedError(AppError):
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    title = "Too many requests"


class BusinessRuleError(AppError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "BUSINESS_RULE_VIOLATION"
    title = "The request violates a business rule"


class DependencyUnavailableError(AppError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "DEPENDENCY_UNAVAILABLE"
    title = "A required dependency is unavailable"


_HTTP_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    412: "PRECONDITION_FAILED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    503: "DEPENDENCY_UNAVAILABLE",
}


def _slug(code: str) -> str:
    return code.lower().replace("_", "-")


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    problem = Problem(
        type=PROBLEM_TYPE_BASE + _slug(code),
        title=title,
        status=status,
        code=code,
        detail=detail,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    if exc.status_code >= 500:
        logger.error("app_error", code=exc.code, detail=exc.detail)
    return problem_response(
        status=exc.status_code, code=exc.code, title=exc.title, detail=exc.detail, headers=exc.headers
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # The submitted input is deliberately not echoed back: it may contain personal data.
    errors = [
        FieldError(loc=list(err.get("loc", ())), msg=str(err.get("msg", "")), type=str(err.get("type", "")))
        for err in exc.errors()
    ]
    return problem_response(
        status=422,
        code="VALIDATION_ERROR",
        title="Request validation failed",
        detail=f"{len(errors)} validation error(s)",
        errors=errors,
    )


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "HTTP error"
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    headers: dict[str, Any] | None = getattr(exc, "headers", None)
    return problem_response(status=exc.status_code, code=code, title=title, detail=detail, headers=headers)


async def _unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", exc_type=type(exc).__name__)
    return problem_response(
        status=500,
        code="INTERNAL",
        title="Internal server error",
        detail="An unexpected error occurred. Quote the request id when contacting support.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
