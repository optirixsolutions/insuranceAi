from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.deps import ActorDep, DbDep, SettingsDep, etag, parse_if_match, require
from app.core.config import Settings
from app.schemas.common import Page
from app.schemas.identity import (
    AccessLinkOut,
    InviteUserRequest,
    InviteUserResponse,
    LoginActivityOut,
    SetRolesRequest,
    UpdateUserRequest,
    UserOut,
)
from app.security.permissions import P
from app.security.principal import Principal
from app.services import user_service
from app.services.user_service import AccessLink, UserView

router = APIRouter(prefix="/users", tags=["users"])
Admin = Annotated[Principal, Depends(require(P.USERS_MANAGE))]


def _out(view: UserView) -> UserOut:
    u = view.user
    return UserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        status=u.status,
        roles=view.role_keys,
        last_login_at=u.last_login_at,
        created_at=u.created_at,
        row_version=u.row_version,
    )


def _link(settings: Settings, link: AccessLink) -> AccessLinkOut:
    # The token travels in the URL fragment, which browsers never send to servers or proxies.
    base = str(settings.app_base_url).rstrip("/")
    return AccessLinkOut(
        url=f"{base}/accept-invite#token={link.token}", purpose=link.purpose, expires_at=link.expires_at
    )


@router.get("", response_model=Page[UserOut])
async def list_users(
    principal: Admin,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_filter: Annotated[Literal["invited", "active", "disabled"] | None, Query(alias="status")] = None,
) -> Page[UserOut]:
    views, total = await user_service.list_users(
        db, principal, page=page, page_size=page_size, q=q, status=status_filter
    )
    return Page(items=[_out(v) for v in views], page=page, page_size=page_size, total=total)


@router.post("", response_model=InviteUserResponse, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: InviteUserRequest, principal: Admin, actor: ActorDep, db: DbDep, settings: SettingsDep
) -> InviteUserResponse:
    """Create an invited user. The returned link is shown once; it is not stored in clear text."""
    view, link = await user_service.invite_user(
        db,
        settings,
        principal,
        actor,
        email=body.email,
        display_name=body.display_name,
        role_keys=set(body.roles),
    )
    return InviteUserResponse(user=_out(view), access_link=_link(settings, link))


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, principal: Admin, db: DbDep, response: Response) -> UserOut:
    view = await user_service.get_user(db, principal, user_id)
    response.headers["ETag"] = etag(view.user.row_version)
    return _out(view)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    principal: Admin,
    actor: ActorDep,
    db: DbDep,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> UserOut:
    view = await user_service.update_user(
        db,
        principal,
        actor,
        user_id,
        expected_version=parse_if_match(if_match),
        display_name=body.display_name,
        status=body.status,
    )
    response.headers["ETag"] = etag(view.user.row_version)
    return _out(view)


@router.put("/{user_id}/roles", response_model=UserOut)
async def set_roles(
    user_id: uuid.UUID, body: SetRolesRequest, principal: Admin, actor: ActorDep, db: DbDep
) -> UserOut:
    return _out(await user_service.set_roles(db, principal, actor, user_id, set(body.roles)))


@router.post("/{user_id}/reset-access", response_model=AccessLinkOut)
async def reset_access(
    user_id: uuid.UUID, principal: Admin, actor: ActorDep, db: DbDep, settings: SettingsDep
) -> AccessLinkOut:
    """Sign the user out everywhere and issue a one-time link to set a new password."""
    return _link(settings, await user_service.reset_access(db, settings, principal, actor, user_id))


@router.get("/{user_id}/login-activity", response_model=list[LoginActivityOut])
async def login_activity(
    user_id: uuid.UUID, principal: Admin, db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[LoginActivityOut]:
    return [
        LoginActivityOut.model_validate(a)
        for a in await user_service.login_activity(db, principal, user_id, limit)
    ]
