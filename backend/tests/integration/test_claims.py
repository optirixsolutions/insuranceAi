"""Claim intake, listing, access control, workflow edits and export through the real API."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from tests.integration.claims_helpers import (
    TODAY,
    claim_payload,
    create_claim,
    make_customer,
    make_policy,
    make_product,
    setup_book,
)
from tests.integration.conftest import ApiClient, accept, invite, register

pytestmark = pytest.mark.integration


def _admin(new_client: Callable[[], ApiClient]) -> ApiClient:
    admin = new_client()
    register(admin)
    return admin


def test_intake_resolves_policy_and_lists_required_documents(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    response = create_claim(admin, claim_payload(customer, policy))
    assert response.status_code == 201, response.text
    claim = response.json()
    assert re.fullmatch(rf"CLM-{TODAY.year}-\d{{6}}", claim["claim_reference"])
    assert claim["status"] == "SUBMITTED" and claim["priority"] == "normal"
    assert claim["claimed_amount"] == {"amount": "5200.00", "currency": "USD"}
    assert claim["policy_version"]["id"] == policy["versions"][0]["id"]
    assert claim["findings"] == []
    assert set(claim["missing_documents"]) == {
        "claim_form",
        "driving_licence",
        "repair_estimate",
        "photo_evidence",
    }
    assert claim["allowed_status_changes"] == ["AWAITING_INFORMATION"]

    second = create_claim(
        admin, claim_payload(customer, policy, incident_date=(TODAY - timedelta(days=40)).isoformat())
    )
    first_no = int(claim["claim_reference"][-6:])
    assert int(second.json()["claim_reference"][-6:]) == first_no + 1


def test_idempotent_submission(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    payload = claim_payload(customer, policy)
    first = create_claim(admin, payload, key="submit-claim-0001")
    replay = create_claim(admin, payload, key="submit-claim-0001")
    assert replay.status_code == 201 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert admin.get("/claims").json()["total"] == 1
    changed = create_claim(
        admin, {**payload, "description": "A different description of the loss."}, key="submit-claim-0001"
    )
    assert changed.status_code == 409
    missing = admin.post("/claims", json=payload)
    assert missing.status_code == 422 and "Idempotency-Key" in missing.json()["detail"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"loss_type": "inpatient"}, "not a valid loss type"),
        ({"incident_date": (TODAY + timedelta(days=1)).isoformat()}, "future"),
        ({"claimed_amount": {"amount": "10.555", "currency": "USD"}}, "decimal place"),
        ({"claimed_amount": {"amount": "0", "currency": "USD"}}, "greater than zero"),
    ],
)
def test_intake_rejects_invalid_claims(
    new_client: Callable[[], ApiClient], overrides: dict[str, Any], message: str
) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    response = create_claim(admin, claim_payload(customer, policy, **overrides))
    assert response.status_code == 422
    assert message in response.text


def test_policy_must_belong_to_customer(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, _, policy = setup_book(admin)
    stranger = make_customer(admin, "Someone Else")
    response = create_claim(admin, claim_payload(stranger, policy))
    assert response.status_code == 422 and "does not belong" in response.json()["detail"]


def test_findings_for_uncovered_dates_and_duplicates(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    before_cover = create_claim(
        admin, claim_payload(customer, policy, incident_date=(TODAY - timedelta(days=300)).isoformat())
    ).json()
    assert before_cover["policy_version"] is None
    assert [f["code"] for f in before_cover["findings"]] == ["NO_POLICY_VERSION", "LATE_REPORT"]

    first = create_claim(admin, claim_payload(customer, policy)).json()
    dup = create_claim(
        admin,
        claim_payload(
            customer,
            policy,
            incident_date=(TODAY - timedelta(days=3)).isoformat(),
            claimed_amount={"amount": "300.00", "currency": "USD"},
        ),
    ).json()
    assert dup["priority"] == "high"
    assert [d["claim_reference"] for d in dup["duplicate_suspects"]] == [first["claim_reference"]]
    related = admin.get(f"/claims/{first['id']}/related").json()
    assert [r["claim_reference"] for r in related] == [dup["claim_reference"]]


def test_listing_search_filters_and_sorting(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    product = make_product(admin)
    alpha, beta = make_customer(admin, "Priya Raman"), make_customer(admin, "Tomasz Nowak")
    pa, pb = make_policy(admin, alpha, product), make_policy(admin, beta, product)
    small = create_claim(
        admin, claim_payload(alpha, pa, claimed_amount={"amount": "250.00", "currency": "USD"})
    ).json()
    big = create_claim(
        admin, claim_payload(beta, pb, claimed_amount={"amount": "19000.00", "currency": "USD"})
    ).json()

    by_name = admin.get("/claims", params={"q": "nowak"}).json()
    assert [c["id"] for c in by_name["items"]] == [big["id"]]
    assert admin.get("/claims", params={"q": small["claim_reference"]}).json()["total"] == 1
    assert admin.get("/claims", params={"q": pa["policy_number"]}).json()["items"][0]["id"] == small["id"]
    ordered = admin.get("/claims", params={"sort": "-priority"}).json()["items"]
    assert [c["priority"] for c in ordered] == ["high", "low"]
    assert admin.get("/claims", params={"priority": "low"}).json()["total"] == 1
    assert admin.get("/claims", params={"assignee": "none"}).json()["total"] == 2
    paged = admin.get("/claims", params={"page_size": 1, "page": 2, "sort": "claimed_amount"}).json()
    assert paged["total"] == 2 and paged["items"][0]["id"] == big["id"]
    assert admin.get("/claims", params={"sort": "drop table"}).status_code == 422


def test_adjusters_only_see_and_edit_assigned_claims(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    mine = create_claim(admin, claim_payload(customer, policy)).json()
    other = create_claim(
        admin, claim_payload(customer, policy, incident_date=(TODAY - timedelta(days=60)).isoformat())
    ).json()
    adjuster = new_client()
    me = accept(adjuster, invite(admin, ["claims_adjuster"]))
    assert adjuster.get("/claims").json()["total"] == 0

    candidates = {u["id"] for u in admin.get("/claims/assignees", params={"role": "adjuster"}).json()}
    assert me["user"]["id"] in candidates
    assert (
        admin.put(
            f"/claims/{mine['id']}/assignments", json={"role": "adjuster", "user_id": me["user"]["id"]}
        ).status_code
        == 200
    )

    visible = adjuster.get("/claims").json()
    assert [c["id"] for c in visible["items"]] == [mine["id"]]
    assert visible["items"][0]["adjuster_name"] == "Invited User"
    assert adjuster.get(f"/claims/{other['id']}").status_code == 404
    detail = adjuster.get(f"/claims/{mine['id']}")
    assert detail.json()["can_edit"] is True
    edited = adjuster.patch(
        f"/claims/{mine['id']}",
        json={"claimed_amount": "6100.00"},
        headers={"If-Match": detail.headers["ETag"]},
    )
    assert edited.status_code == 200 and edited.json()["claimed_amount"]["amount"] == "6100.00"
    assert (
        adjuster.put(
            f"/claims/{mine['id']}/assignments", json={"role": "adjuster", "user_id": None}
        ).status_code
        == 403
    )  # cannot reassign


def test_assignee_must_hold_the_matching_permission(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    claim = create_claim(admin, claim_payload(customer, policy)).json()
    reviewer = accept(new_client(), invite(admin, ["reviewer"]))
    response = admin.put(
        f"/claims/{claim['id']}/assignments", json={"role": "adjuster", "user_id": reviewer["user"]["id"]}
    )
    assert response.status_code == 422 and "claims.write" in response.json()["detail"]
    assert (
        admin.put(
            f"/claims/{claim['id']}/assignments", json={"role": "reviewer", "user_id": reviewer["user"]["id"]}
        ).status_code
        == 200
    )


def test_status_changes_follow_the_state_machine(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    claim = create_claim(admin, claim_payload(customer, policy)).json()
    cid = claim["id"]
    etag = admin.get(f"/claims/{cid}").headers["ETag"]
    approve = admin.post(
        f"/claims/{cid}/status",
        json={"status": "APPROVED", "reason": "Looks fine to me"},
        headers={"If-Match": etag},
    )
    assert approve.status_code == 422 and "SUBMITTED to APPROVED" in approve.json()["detail"]
    moved = admin.post(
        f"/claims/{cid}/status",
        json={"status": "AWAITING_INFORMATION", "reason": "Repair estimate missing"},
        headers={"If-Match": etag},
    )
    assert moved.status_code == 200 and moved.json()["allowed_status_changes"] == ["SUBMITTED"]
    stale = admin.post(
        f"/claims/{cid}/status",
        json={"status": "SUBMITTED", "reason": "Estimate received"},
        headers={"If-Match": etag},
    )
    assert stale.status_code == 412
    locked = admin.patch(
        f"/claims/{cid}",
        json={"description": "Updated after the customer called in."},
        headers={"If-Match": admin.get(f"/claims/{cid}").headers["ETag"]},
    )
    assert locked.status_code == 200  # descriptive fields stay editable

    admin.post(f"/claims/{cid}/notes", json={"body": "Called the customer; estimate due Friday."})
    kinds = [e["kind"] for e in admin.get(f"/claims/{cid}/timeline").json()]
    assert kinds.count("status") == 2 and "note" in kinds


def test_restricted_notes(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    _, customer, policy = setup_book(admin)
    claim = create_claim(admin, claim_payload(customer, policy)).json()
    adjuster = new_client()
    me = accept(adjuster, invite(admin, ["claims_adjuster"]))
    admin.put(f"/claims/{claim['id']}/assignments", json={"role": "adjuster", "user_id": me["user"]["id"]})
    admin.post(
        f"/claims/{claim['id']}/notes",
        json={"body": "Possible link to an earlier SIU case.", "visibility": "restricted"},
    )
    adjuster.post(f"/claims/{claim['id']}/notes", json={"body": "Photos requested from the customer."})
    assert (
        adjuster.post(
            f"/claims/{claim['id']}/notes", json={"body": "x", "visibility": "restricted"}
        ).status_code
        == 403
    )
    assert [n["visibility"] for n in adjuster.get(f"/claims/{claim['id']}/notes").json()] == ["internal"]
    assert len(admin.get(f"/claims/{claim['id']}/notes").json()) == 2


def test_claims_are_invisible_across_organisations(new_client: Callable[[], ApiClient]) -> None:
    org_a, org_b = _admin(new_client), _admin(new_client)
    _, customer, policy = setup_book(org_a)
    claim = create_claim(org_a, claim_payload(customer, policy)).json()
    assert org_b.get(f"/claims/{claim['id']}").status_code == 404
    assert org_b.get("/claims").json()["total"] == 0
    assert org_b.get(f"/policies/{policy['id']}").status_code == 404
    hijack = create_claim(org_b, claim_payload(customer, policy))
    assert hijack.status_code == 404  # org B cannot file against org A's policy


def test_export_neutralises_spreadsheet_formulas(new_client: Callable[[], ApiClient]) -> None:
    admin = _admin(new_client)
    product = make_product(admin)
    customer = make_customer(admin, '=HYPERLINK("http://evil.example")')
    policy = make_policy(admin, customer, product)
    claim = create_claim(admin, claim_payload(customer, policy)).json()
    response = admin.get("/claims/export")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["claim_reference"] == claim["claim_reference"]
    assert rows[0]["customer"].startswith("'=")
