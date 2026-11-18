"""Shared FastAPI dependencies: database session, authentication, CSRF and permissions."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import AppError, ForbiddenError, PreconditionRequiredError, UnauthenticatedError
from app.core.request_context import get_request_id
from app.security.permissions import P
from app.security.principal import Principal
from app.security.throttle import LoginThrottle
from app.security.tokens import tokens_match
from app.services import auth_service
from app.services.audit_service import AuditActor
from app.services.auth_service import ClientInfo, IssuedSession

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"


class CsrfError(AppError):
    status_code = HTTPStatus.FORBIDDEN
    code = "CSRF_FAILED"
    title = "Missing or invalid CSRF token"


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.db
    async with database.sessionmaker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_throttle(settings: SettingsDep, redis: Annotated[Redis, Depends(get_redis)]) -> LoginThrottle:
    return LoginThrottle(
        redis,
        max_account_failures=settings.login_max_account_failures,
        max_ip_failures=settings.login_max_ip_failures,
        window_seconds=settings.login_lockout_seconds,
    )


def _client_ip(request: Request) -> str | None:
    """The peer address, only if it is a real IP (test clients and Unix sockets are not)."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def client_info(request: Request) -> ClientInfo:
    return ClientInfo(
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=get_request_id(),
    )


ClientDep = Annotated[ClientInfo, Depends(client_info)]


def session_cookie_name(settings: Settings) -> str:
    return "__Host-claimos_session" if settings.session_cookie_secure else "claimos_session"


def csrf_cookie_name(settings: Settings) -> str:
    return "__Host-claimos_csrf" if settings.session_cookie_secure else "claimos_csrf"


def set_session_cookies(response: Response, settings: Settings, issued: IssuedSession) -> None:
    max_age = int(settings.session_absolute_hours * 3600)
    secure = settings.session_cookie_secure
    response.set_cookie(
        session_cookie_name(settings),
        issued.token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    # Readable by the frontend, which echoes it in the X-CSRF-Token header (double submit).
    response.set_cookie(
        csrf_cookie_name(settings),
        issued.csrf_token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (session_cookie_name(settings), csrf_cookie_name(settings)):
        response.delete_cookie(name, path="/", secure=settings.session_cookie_secure, httponly=True)


async def get_principal(request: Request, db: DbDep, settings: SettingsDep) -> Principal:
    token = request.cookies.get(session_cookie_name(settings))
    if not token:
        raise UnauthenticatedError("Sign in to continue")
    principal = await auth_service.resolve_session(db, settings, token)
    if principal is None:
        raise UnauthenticatedError("Your session has ended. Sign in again.")
    if request.method not in SAFE_METHODS:
        supplied = request.headers.get(CSRF_HEADER)
        if not supplied or not tokens_match(supplied, principal.csrf_hash):
            raise CsrfError()
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(permission: P) -> Callable[[Principal], Awaitable[Principal]]:
    async def dependency(principal: PrincipalDep) -> Principal:
        if principal.mfa_enrollment_required:
            raise ForbiddenError("Your organisation requires two-factor authentication: set it up first")
        if not principal.has(permission.value):
            raise ForbiddenError(f"This action requires the '{permission.value}' permission")
        return principal

    return dependency


def audit_actor(principal: PrincipalDep, client: ClientDep) -> AuditActor:
    return AuditActor("user", principal.user_id, client.request_id, client.ip)


ActorDep = Annotated[AuditActor, Depends(audit_actor)]


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise PreconditionRequiredError("Send the ETag from the last read in an If-Match header")
    cleaned = value.strip().removeprefix("W/").strip('"')
    if not cleaned.isdigit():
        raise PreconditionRequiredError("If-Match must contain the resource version from its ETag")
    return int(cleaned)


def etag(version: int) -> str:
    return f'"{version}"'
