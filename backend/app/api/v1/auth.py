from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ClientDep,
    DbDep,
    PrincipalDep,
    SettingsDep,
    clear_session_cookies,
    get_throttle,
    set_session_cookies,
)
from app.core.config import Settings
from app.core.errors import UnauthenticatedError
from app.models.identity import Organization
from app.schemas.identity import (
    AcceptAccessRequest,
    ChangePasswordRequest,
    LoginRequest,
    MeOrganization,
    MeResponse,
    MeUser,
    RegisterRequest,
    SessionOut,
)
from app.security.principal import Principal
from app.security.throttle import LoginThrottle
from app.services import auth_service
from app.services.auth_service import IssuedSession

router = APIRouter(prefix="/auth", tags=["auth"])


class MfaChallengeOut(BaseModel):
    mfa_required: bool
    challenge: str


async def _me(db: AsyncSession, principal: Principal) -> MeResponse:
    org = await db.get(Organization, principal.organization_id)
    if org is None:
        raise UnauthenticatedError("Organisation not found")
    return MeResponse(
        user=MeUser(id=principal.user_id, email=principal.email, display_name=principal.display_name),
        organization=MeOrganization(id=org.id, name=org.name, slug=org.slug),
        roles=sorted(principal.role_keys),
        permissions=sorted(principal.permissions),
    )


async def _signed_in(
    db: AsyncSession, settings: Settings, response: Response, issued: IssuedSession
) -> MeResponse:
    principal = await auth_service.resolve_session(db, settings, issued.token)
    if principal is None:  # pragma: no cover - a just-issued session is always valid
        raise UnauthenticatedError("Session could not be established")
    set_session_cookies(response, settings, issued)
    me = await _me(db, principal)
    me.session_expires_at = issued.absolute_expires_at
    return me


@router.post("/register", response_model=MeResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, response: Response, db: DbDep, settings: SettingsDep, client: ClientDep
) -> MeResponse:
    """Create an organisation and its first administrator, then sign them in."""
    issued = await auth_service.register(
        db,
        settings,
        organization_name=body.organization_name,
        display_name=body.display_name,
        email=body.email,
        password=body.password,
        client=client,
    )
    return await _signed_in(db, settings, response, issued)


@router.post("/login", response_model=MeResponse | MfaChallengeOut)
async def login(
    body: LoginRequest,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    client: ClientDep,
    throttle: Annotated[LoginThrottle, Depends(get_throttle)],
) -> MeResponse | MfaChallengeOut:
    """A correct password signs in, or, when the user has two-factor authentication on, returns a
    short-lived challenge for ``POST /auth/mfa/verify`` (no session or cookie is issued yet)."""
    issued = await auth_service.login(
        db, settings, throttle, email=body.email, password=body.password, client=client
    )
    if isinstance(issued, auth_service.MfaRequired):
        return MfaChallengeOut(mfa_required=True, challenge=issued.challenge)
    return await _signed_in(db, settings, response, issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    principal: PrincipalDep, response: Response, db: DbDep, settings: SettingsDep, client: ClientDep
) -> None:
    await auth_service.logout(db, principal, client)
    clear_session_cookies(response, settings)


@router.get("/me", response_model=MeResponse)
async def me(principal: PrincipalDep, db: DbDep) -> MeResponse:
    return await _me(db, principal)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest, principal: PrincipalDep, db: DbDep, client: ClientDep
) -> None:
    """Change the caller's password and sign out every other session."""
    await auth_service.change_password(
        db, principal, current_password=body.current_password, new_password=body.new_password, client=client
    )


@router.post("/invitations/accept", response_model=MeResponse)
async def accept_invitation(
    body: AcceptAccessRequest, response: Response, db: DbDep, settings: SettingsDep, client: ClientDep
) -> MeResponse:
    """Set a password from an invitation or access-reset link and sign in."""
    issued = await auth_service.accept_access_token(
        db, settings, token=body.token, password=body.password, client=client
    )
    return await _signed_in(db, settings, response, issued)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(principal: PrincipalDep, db: DbDep) -> list[SessionOut]:
    sessions = await auth_service.list_sessions(db, principal)
    out = [SessionOut.model_validate(s) for s in sessions]
    for item in out:
        item.current = item.id == principal.session_id
    return out


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, request: Request, principal: PrincipalDep, db: DbDep, client: ClientDep
) -> None:
    await auth_service.revoke_own_session(db, principal, session_id, client)
