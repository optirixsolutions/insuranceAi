"""Two-factor authentication endpoints (Phase 13). See app.services.mfa_service."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ActorDep, ClientDep, DbDep, PrincipalDep, SettingsDep, require
from app.models.identity import Organization
from app.security.permissions import P
from app.security.principal import Principal
from app.services import auth_service, mfa_service

router = APIRouter(tags=["mfa"])
OrgAdmin = Annotated[Principal, Depends(require(P.ORG_MANAGE))]
UserAdmin = Annotated[Principal, Depends(require(P.USERS_MANAGE))]
Code = Annotated[str, Field(min_length=6, max_length=20)]


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Code


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: Annotated[str, Field(min_length=40, max_length=120)]
    code: Code


class DisableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: Annotated[str, Field(min_length=1, max_length=200)]
    code: Code


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_roles: Annotated[list[str], Field(max_length=10)]


@router.post("/auth/mfa/verify")
async def verify(
    body: VerifyIn, response: Response, db: DbDep, settings: SettingsDep, client: ClientDep
) -> Any:
    """Second sign-in step: a code from the authenticator app or a recovery code."""
    from app.api.v1.auth import _signed_in

    issued = await auth_service.complete_mfa(
        db, settings, challenge=body.challenge, code=body.code, client=client
    )
    return await _signed_in(db, settings, response, issued)


@router.get("/auth/mfa")
async def mfa_status(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return await mfa_service.status(db, principal)


@router.post("/auth/mfa/setup")
async def setup(principal: PrincipalDep, db: DbDep, settings: SettingsDep) -> dict[str, str]:
    """A new secret for the authenticator app (shown once). Confirm it with a code to turn MFA on."""
    out = await mfa_service.setup(db, principal, settings)
    await db.commit()
    return out


@router.post("/auth/mfa/confirm")
async def confirm(
    body: CodeIn, principal: PrincipalDep, actor: ActorDep, db: DbDep, settings: SettingsDep
) -> dict[str, list[str]]:
    codes = await mfa_service.confirm(db, principal, actor, settings, body.code)
    await db.commit()
    return {"recovery_codes": codes}


@router.post("/auth/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable(
    body: DisableIn, principal: PrincipalDep, actor: ActorDep, db: DbDep, settings: SettingsDep
) -> Response:
    await mfa_service.disable(db, principal, actor, settings, password=body.password, code=body.code)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/mfa/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset(user_id: uuid.UUID, principal: UserAdmin, actor: ActorDep, db: DbDep) -> Response:
    await mfa_service.admin_reset(db, principal, actor, user_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organization/mfa-policy")
async def get_policy(principal: OrgAdmin, db: DbDep) -> dict[str, list[str]]:
    org = await db.get(Organization, principal.organization_id)
    return {"required_roles": list(org.mfa_required_roles or []) if org else []}


@router.put("/organization/mfa-policy")
async def put_policy(body: PolicyIn, principal: OrgAdmin, actor: ActorDep, db: DbDep) -> dict[str, list[str]]:
    roles = await mfa_service.set_policy(db, principal, actor, body.required_roles)
    await db.commit()
    return {"required_roles": roles}
