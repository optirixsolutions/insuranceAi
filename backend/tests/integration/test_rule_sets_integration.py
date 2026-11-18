"""Rule-set versioning on real PostgreSQL: drafts, validation, publish/retire, immutability, permissions."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.claims_helpers import make_product
from tests.integration.conftest import ApiClient, accept, invite, register

pytestmark = pytest.mark.integration


async def test_rule_set_lifecycle(new_client: Callable[[], ApiClient], migrator_engine: AsyncEngine) -> None:
    admin = new_client()
    register(admin)
    product = make_product(admin)
    defaults = admin.get("/rule-sets/defaults").json()
    assert defaults["config"]["stp"]["enabled"] is False and "properties" in defaults["schema"]

    first = admin.post(f"/products/{product['id']}/rule-sets", json={"change_note": "initial"})
    assert first.status_code == 201 and first.json()["status"] == "draft" and first.json()["version_no"] == 1
    assert (
        admin.post(f"/products/{product['id']}/rule-sets", json={}).status_code == 409
    )  # one draft at a time
    rid = first.json()["id"]

    bad = {**first.json()["config"], "stp": {"enabled": True, "max_amount": "5000"}}
    r = admin.patch(f"/rule-sets/{rid}", json={"config": bad}, headers={"If-Match": '"1"'})
    assert r.status_code == 422 and "compliance profile" in r.json()["detail"]
    good = {**bad, "automated_approval_allowed": True}
    r = admin.patch(f"/rule-sets/{rid}", json={"config": good}, headers={"If-Match": '"1"'})
    assert r.status_code == 200 and r.json()["config"]["stp"]["enabled"] is True
    stale = admin.patch(f"/rule-sets/{rid}", json={"config": good}, headers={"If-Match": '"1"'})
    assert stale.status_code == 409

    published = admin.post(f"/rule-sets/{rid}/publish", headers={"If-Match": '"2"'})
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert (
        admin.patch(f"/rule-sets/{rid}", json={"config": good}, headers={"If-Match": '"3"'}).status_code
        == 409
    )

    second = admin.post(f"/products/{product['id']}/rule-sets", json={"change_note": "v2"}).json()
    assert (
        second["version_no"] == 2 and second["config"] == published.json()["config"]
    )  # copied from published
    admin.post(f"/rule-sets/{second['id']}/publish", headers={"If-Match": '"1"'})
    versions = {
        v["version_no"]: v["status"] for v in admin.get(f"/products/{product['id']}/rule-sets").json()
    }
    assert versions == {2: "published", 1: "retired"}

    async with migrator_engine.begin() as conn:  # even the schema owner cannot rewrite a published version
        with pytest.raises(DBAPIError, match="immutable"):
            await conn.execute(
                text("UPDATE rule_sets SET config = '{}'::jsonb WHERE id = :id"), {"id": second["id"]}
            )
    async with migrator_engine.begin() as conn:
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            await conn.execute(text("DELETE FROM rule_sets WHERE id = :id"), {"id": rid})


async def test_only_publishers_publish(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    product = make_product(admin)
    draft = admin.post(f"/products/{product['id']}/rule-sets", json={}).json()
    manager = new_client()
    accept(manager, invite(admin, ["claims_manager"]))
    assert manager.post(f"/rule-sets/{draft['id']}/publish", headers={"If-Match": '"1"'}).status_code == 403
    other = new_client()
    register(other)
    assert other.get(f"/rule-sets/{draft['id']}").status_code == 404
