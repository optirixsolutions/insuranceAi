from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep
from app.models.identity import Permission, Role
from app.repositories import identity as repo
from app.schemas.identity import PermissionOut, RoleOut

router = APIRouter(tags=["roles"])


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(principal: PrincipalDep, db: DbDep) -> list[RoleOut]:
    roles = (
        await db.scalars(
            select(Role)
            .where(Role.organization_id == principal.organization_id)
            .order_by(Role.is_system.desc(), Role.name)
        )
    ).all()
    perms = await repo.role_permissions(db, principal.organization_id)
    return [
        RoleOut(
            id=r.id,
            key=r.key,
            name=r.name,
            description=r.description,
            is_system=r.is_system,
            permissions=sorted(perms.get(r.id, frozenset())),
        )
        for r in roles
    ]


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(_: PrincipalDep, db: DbDep) -> list[PermissionOut]:
    rows = (await db.scalars(select(Permission).order_by(Permission.key))).all()
    return [PermissionOut(key=p.key, description=p.description) for p in rows]
