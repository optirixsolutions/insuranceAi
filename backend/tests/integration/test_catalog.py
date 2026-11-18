"""Products, customers and versioned policies against the real database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.claims_helpers import (
    TODAY,
    make_customer,
    make_policy,
    make_product,
    version_payload,
)
from tests.integration.conftest import ApiClient, register

pytestmark = pytest.mark.integration


def test_product_gets_line_specific_document_requirements(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    product = make_product(admin, "health")
    mandatory = {r["document_type"] for r in product["document_requirements"] if r["mandatory"]}
    assert mandatory == {"claim_form", "identity_document", "hospital_report", "medical_bill"}
    dup = admin.post(
        "/products", json={"code": product["code"], "name": "x", "line": "health", "currency": "USD"}
    )
    assert dup.status_code == 409
    bad = admin.post("/products", json={"code": "NEW-1", "name": "x", "line": "motor", "currency": "XYZ"})
    assert bad.status_code == 422


def test_policy_versions_cannot_overlap_and_can_be_superseded(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    product, customer = make_product(admin), make_customer(admin)
    start = TODAY - timedelta(days=100)
    policy = make_policy(admin, customer, product, start=start, end=start + timedelta(days=365))
    v1 = policy["versions"][0]
    assert v1["status"] == "published" and v1["sum_insured"] == "20000.00"

    # Overlapping draft cannot be published while v1 stands.
    overlap = admin.post(
        f"/policies/{policy['id']}/versions",
        json=version_payload(start + timedelta(days=50), start + timedelta(days=365), "30000.00"),
    )
    assert overlap.status_code == 201
    v2_id = overlap.json()["id"]
    refused = admin.post(f"/policies/{policy['id']}/versions/{v2_id}/publish")
    assert refused.status_code == 409
    assert "overlaps" in refused.json()["detail"]

    # Mid-term change: publish a shortened copy of v1 and the new terms, superseding v1.
    head = admin.post(
        f"/policies/{policy['id']}/versions", json=version_payload(start, start + timedelta(days=50))
    ).json()
    assert (
        admin.post(
            f"/policies/{policy['id']}/versions/{head['id']}/publish",
            json={"supersede_version_ids": [v1["id"]]},
        ).status_code
        == 200
    )
    assert admin.post(f"/policies/{policy['id']}/versions/{v2_id}/publish").status_code == 200
    statuses = {
        v["version_no"]: v["status"] for v in admin.get(f"/policies/{policy['id']}").json()["versions"]
    }
    assert statuses == {1: "superseded", 2: "published", 3: "published"}


def test_version_validation(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    product, customer = make_product(admin), make_customer(admin)
    policy = make_policy(admin, customer, product, publish=False)
    pid = policy["id"]
    payload = version_payload(TODAY, TODAY - timedelta(days=1))
    assert admin.post(f"/policies/{pid}/versions", json=payload).status_code == 422  # end before start
    payload = version_payload(TODAY, TODAY + timedelta(days=30))
    payload["coverages"][0]["clause_ref"] = "9.9"
    response = admin.post(f"/policies/{pid}/versions", json=payload)
    assert response.status_code == 422 and "unknown clauses" in response.json()["detail"]
    payload = version_payload(TODAY, TODAY + timedelta(days=30), sum_insured="100.555")
    assert admin.post(f"/policies/{pid}/versions", json=payload).status_code == 422  # over-precise USD


async def test_published_terms_are_immutable_in_the_database(
    new_client: Callable[[], ApiClient], app_engine: AsyncEngine
) -> None:
    admin = new_client()
    me = register(admin)
    policy = make_policy(admin, make_customer(admin), make_product(admin))
    version_id = policy["versions"][0]["id"]
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": me["organization"]["id"]})
        with pytest.raises(DBAPIError, match="immutable"):
            await conn.execute(
                text("UPDATE policy_versions SET sum_insured = 1 WHERE id = :v"), {"v": version_id}
            )
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": me["organization"]["id"]})
        with pytest.raises(DBAPIError, match="immutable"):
            await conn.execute(
                text("UPDATE policy_coverages SET limit_amount = 1 WHERE policy_version_id = :v"),
                {"v": version_id},
            )


def test_policy_import_is_all_or_nothing(new_client: Callable[[], ApiClient]) -> None:
    admin = new_client()
    register(admin)
    product = make_product(admin)
    start = TODAY - timedelta(days=30)

    def item(number: str, product_code: str) -> dict[str, object]:
        return {
            "policy_number": number,
            "product_code": product_code,
            "is_synthetic": True,
            "customer": {
                "external_ref": f"IMP-{number}",
                "full_name": f"Imported {number}",
                "is_synthetic": True,
            },
            "versions": [version_payload(start, start + timedelta(days=365))],
        }

    good = {"policies": [item("IMP-0001", product["code"]), item("IMP-0002", product["code"])]}
    dry = admin.post("/policies/import", json={**good, "dry_run": True})
    assert dry.status_code == 200 and dry.json()["policies_created"] == 2
    assert admin.get("/policies", params={"q": "IMP-000"}).json()["total"] == 0  # dry run saved nothing

    broken = {"dry_run": False, "policies": [item("IMP-0003", product["code"]), item("IMP-0004", "NO-SUCH")]}
    rejected = admin.post("/policies/import", json=broken)
    assert rejected.status_code == 422 and "nothing was saved" in rejected.json()["detail"]
    assert admin.get("/policies", params={"q": "IMP-0003"}).json()["total"] == 0

    real = admin.post("/policies/import", json={**good, "dry_run": False})
    assert real.json() == {
        "dry_run": False,
        "policies_created": 2,
        "customers_created": 2,
        "versions_created": 2,
        "versions_published": 2,
        "policy_numbers": ["IMP-0001", "IMP-0002"],
    }
    assert admin.get("/policies", params={"q": "IMP-000"}).json()["total"] == 2


def test_catalog_permissions(new_client: Callable[[], ApiClient]) -> None:
    from tests.integration.conftest import accept, invite

    admin = new_client()
    register(admin)
    adjuster = new_client()
    accept(adjuster, invite(admin, ["claims_adjuster"]))
    assert (
        adjuster.post(
            "/products", json={"code": "X-1", "name": "x", "line": "motor", "currency": "USD"}
        ).status_code
        == 403
    )
    assert adjuster.post("/customers", json={"full_name": "X"}).status_code == 403
    assert adjuster.get("/customers").status_code == 200  # read access for claim handling
