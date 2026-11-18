from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from app.api.deps import ActorDep, DbDep, PrincipalDep, etag, parse_if_match, require
from app.core.errors import NotFoundError, PreconditionFailedError
from app.models.identity import Organization
from app.schemas.identity import OrganizationOut, OrganizationUpdate
from app.security.permissions import P
from app.security.principal import Principal
from app.services import audit_service

router = APIRouter(prefix="/organization", tags=["organization"])


@router.get("", response_model=OrganizationOut)
async def get_organization(principal: PrincipalDep, db: DbDep, response: Response) -> OrganizationOut:
    org = await db.get(Organization, principal.organization_id)
    if org is None:
        raise NotFoundError("Organisation not found")
    response.headers["ETag"] = etag(org.row_version)
    return OrganizationOut.model_validate(org)


@router.patch("", response_model=OrganizationOut)
async def update_organization(
    body: OrganizationUpdate,
    principal: Annotated[Principal, Depends(require(P.ORG_MANAGE))],
    actor: ActorDep,
    db: DbDep,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> OrganizationOut:
    expected = parse_if_match(if_match)
    org = await db.get(Organization, principal.organization_id, with_for_update=True)
    if org is None:
        raise NotFoundError("Organisation not found")
    if org.row_version != expected:
        raise PreconditionFailedError(
            "Organisation settings were changed by someone else. Reload and try again."
        )
    changes = body.model_dump(exclude_unset=True)
    before = {k: getattr(org, k) for k in changes}
    for key, value in changes.items():
        setattr(org, key, value)
    if changes and before != changes:
        org.row_version += 1
        await audit_service.record(
            db,
            actor=actor,
            action="organization.updated",
            entity_type="organization",
            entity_id=org.id,
            before=before,
            after=changes,
        )
    await db.commit()
    await db.refresh(org)
    response.headers["ETag"] = etag(org.row_version)
    return OrganizationOut.model_validate(org)
