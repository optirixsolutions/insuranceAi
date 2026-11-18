"""Human review on real PostgreSQL + Redis: every review transition and every denied path.

A claim reaches HUMAN_REVIEW through the real graph (no documents -> MORE_INFO -> "escalate" ->
HUMAN_REVIEW). Where a test needs a specific recommendation, payout or fraud level, the persisted
review row is set with the migrator role before the decision; the decision path itself is real.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.services import workflow_service
from tests.integration.claims_helpers import claim_payload, create_claim, setup_book
from tests.integration.conftest import ApiClient, accept, invite, register

pytestmark = pytest.mark.integration
TODAY = date.today()
Factory = Callable[[], ApiClient]


@dataclass
class Case:
    org: uuid.UUID
    claim_id: str
    execution_id: str
    review_id: str

    def url(self, action: str = "") -> str:
        return f"/claims/{self.claim_id}/reviews/{self.review_id}" + (f"/{action}" if action else "")


def _key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _to_review(admin: ApiClient, settings: Settings) -> Case:
    org = uuid.UUID(admin.get("/auth/me").json()["organization"]["id"])
    _, customer, policy = setup_book(admin)
    created = create_claim(admin, claim_payload(customer, policy))
    assert created.status_code == 201, created.text
    claim_id = created.json()["id"]
    started = admin.post(f"/claims/{claim_id}/processing", headers=_key())
    assert started.status_code == 202, started.text
    execution_id = uuid.UUID(started.json()["id"])
    assert await workflow_service.run(settings, org, execution_id, None, None) == "interrupted"
    pending = admin.get(f"/workflow-executions/{execution_id}").json()["interrupt"]
    url = f"/workflow-executions/{execution_id}/resume"
    ok = admin.post(url, json={"interrupt_id": pending["interrupt_id"], "action": "escalate"})
    assert ok.status_code == 202, ok.text
    resume = {"action": "escalate", "note": "", "by": "t", "interrupt_id": pending["interrupt_id"]}
    assert await workflow_service.run(settings, org, execution_id, None, resume) == "interrupted"
    interrupt = admin.get(f"/workflow-executions/{execution_id}").json()["interrupt"]
    assert interrupt["kind"] == "human_review"
    return Case(org, claim_id, str(execution_id), interrupt["review_id"])


async def _resume_worker(settings: Settings, case: Case, admin: ApiClient) -> str:
    """What the Celery worker does with the resume the review API enqueued."""
    interrupt = admin.get(f"/workflow-executions/{case.execution_id}").json()["interrupt"]
    payload = {
        "action": interrupt["resumed_with"],
        "note": "",
        "by": interrupt["resumed_by"],
        "interrupt_id": interrupt["interrupt_id"],
    }
    return await workflow_service.run(settings, case.org, uuid.UUID(case.execution_id), None, payload)


def _member(new_client: Factory, admin: ApiClient, roles: list[str]) -> tuple[ApiClient, str]:
    client = new_client()
    body = accept(client, invite(admin, roles))
    return client, str(body["user"]["id"])


def _grant(granter: ApiClient, user_id: str, *, limit: str = "10000", **kw: Any) -> None:
    item = {
        "max_approval_amount": limit,
        "currency": "USD",
        "valid_from": (TODAY - timedelta(days=1)).isoformat(),
        **kw,
    }
    r = granter.put(f"/users/{user_id}/authorities", json=[item])
    assert r.status_code == 200, r.text


async def _set(engine: AsyncEngine, review_id: str, **values: Any) -> None:
    assert set(values) <= {"recommendation", "payout", "fraud_level"}  # fixed column names only
    sets = ", ".join(f"{k} = :{k}" for k in values)
    async with engine.begin() as c:
        statement = text(f"UPDATE human_reviews SET {sets} WHERE id = :id")  # noqa: S608
        await c.execute(statement, {"id": review_id, **values})


def _approve(client: ApiClient, case: Case, amount: str, **kw: Any) -> Any:
    body = {"approved_amount": amount, "reason": "damage consistent with photos", **kw}
    return client.post(case.url("approve"), json=body, headers=_key())


def _code(r: Any) -> tuple[int, str]:
    return r.status_code, r.json().get("code", "")


# --- tests ---------------------------------------------------------------------------------------


async def test_review_is_opened_listed_and_only_decided_through_the_review_api(
    new_client: Factory, settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    reviewer, _ = _member(new_client, admin, ["reviewer"])
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "HUMAN_REVIEW"

    queue = reviewer.get("/reviews").json()  # unassigned reviewers see the shared queue
    assert [q["id"] for q in queue] == [case.review_id]
    assert queue[0]["status"] == "pending" and queue[0]["due_at"] and not queue[0]["overdue"]
    workspace = reviewer.get(case.url())
    assert workspace.status_code == 200, workspace.text
    ws = workspace.json()
    assert ws["waiting"] and {"approve", "reject", "request_information", "escalate"} <= set(ws["actions"])

    interrupt = admin.get(f"/workflow-executions/{case.execution_id}").json()["interrupt"]
    generic = admin.post(
        f"/workflow-executions/{case.execution_id}/resume",
        json={"interrupt_id": interrupt["interrupt_id"], "action": "request_information"},
    )
    assert generic.status_code == 422 and "review workspace" in generic.json()["detail"]

    adjuster, _ = _member(new_client, admin, ["claims_adjuster"])
    assert adjuster.get("/reviews").status_code == 403
    assert adjuster.get(case.url()).status_code == 404  # not assigned and cannot decide: invisible


async def test_approval_within_authority_completes_the_claim_and_is_idempotent(
    new_client: Factory, settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    reviewer, rid = _member(new_client, admin, ["reviewer"])
    _grant(admin, rid)
    assert reviewer.post(case.url("claim")).json()["status"] == "in_progress"
    assert reviewer.get("/reviews", params={"assignee": "me"}).json()[0]["assigned_to"] == rid

    key = {"Idempotency-Key": uuid.uuid4().hex}
    body = {"approved_amount": "800.00", "reason": "repair estimate verified"}
    first = reviewer.post(case.url("approve"), json=body, headers=key)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "completed" and first.json()["outcome"] == "approved"
    replay = reviewer.post(case.url("approve"), json=body, headers=key)
    assert replay.status_code == 200 and replay.headers.get("Idempotent-Replayed") == "true"
    assert reviewer.post(case.url("approve"), json=body, headers=_key()).status_code == 409

    decisions = admin.get(f"/claims/{case.claim_id}/decisions").json()
    assert len(decisions) == 1
    d = decisions[0]
    assert (d["decision"], d["decision_mode"], d["decided_by"]) == ("approved", "human", rid)
    assert d["approved_amount"] == "800.0000" and d["review_id"] == case.review_id
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "APPROVED"
    assert await _resume_worker(settings, case, admin) == "completed"
    assert len(admin.get("/reviews", params={"status": "completed"}).json()) == 1  # no second review


async def test_denied_paths(new_client: Factory, settings: Settings, migrator_engine: AsyncEngine) -> None:
    admin = new_client()
    me = register(admin)
    case = await _to_review(admin, settings)
    reviewer, rid = _member(new_client, admin, ["reviewer"])

    no_key = reviewer.post(case.url("approve"), json={"approved_amount": "10", "reason": "x"})
    assert no_key.status_code == 422
    assert _code(_approve(reviewer, case, "800.00")) == (403, "AUTHORITY_EXCEEDED")  # no authority
    _grant(admin, rid, limit="500")
    denied = _approve(reviewer, case, "800.00")
    assert _code(denied) == (403, "AUTHORITY_EXCEEDED") and "exceeds" in denied.json()["detail"]
    reject = {"reason_code": "EXCLUSION", "reason": "excluded", "clause_ids": ["4.2"]}
    rej = reviewer.post(case.url("reject"), json=reject, headers=_key())
    assert _code(rej) == (403, "AUTHORITY_EXCEEDED") and "reject" in rej.json()["detail"]

    await _set(migrator_engine, case.review_id, fraud_level="high")
    _grant(admin, rid, limit="10000")
    risky = _approve(reviewer, case, "800.00")
    assert _code(risky) == (403, "AUTHORITY_EXCEEDED") and "fraud-override" in risky.json()["detail"]
    await _set(migrator_engine, case.review_id, fraud_level="low", recommendation="REJECT_RECOMMENDED")
    no_reason = _approve(reviewer, case, "800.00")
    assert _code(no_reason) == (422, "BUSINESS_RULE_VIOLATION")
    assert "override reason" in no_reason.json()["detail"]

    # segregation of duties: the claim's adjuster (here the admin) cannot decide it
    assign = admin.put(
        f"/claims/{case.claim_id}/assignments", json={"role": "adjuster", "user_id": me["user"]["id"]}
    )
    assert assign.status_code == 200, assign.text
    peer, _ = _member(new_client, admin, ["org_admin"])
    _grant(peer, me["user"]["id"])  # nobody can grant their own authority
    own = _approve(admin, case, "800.00", override_reason="photos confirm")
    assert _code(own) == (403, "AUTHORITY_EXCEEDED") and "assigned" in own.json()["detail"]

    assert reviewer.post(case.url("claim")).status_code == 200
    other, oid = _member(new_client, admin, ["reviewer"])
    _grant(admin, oid)
    assert other.post(case.url("claim")).status_code == 409  # someone else is working on it
    adjuster, _ = _member(new_client, admin, ["claims_adjuster"])
    sneaky = adjuster.post(case.url("approve"), json={"approved_amount": "1", "reason": "x"}, headers=_key())
    assert _code(sneaky) == (403, "FORBIDDEN")  # needs reviews.decide before anything else
    assert admin.get(f"/claims/{case.claim_id}/decisions").json() == []
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "HUMAN_REVIEW"


async def test_four_eyes_needs_a_different_authorised_second_approver(
    new_client: Factory, settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    first, fid = _member(new_client, admin, ["reviewer"])
    weak, wid = _member(new_client, admin, ["reviewer"])
    second, sid = _member(new_client, admin, ["reviewer"])
    _grant(admin, fid)
    _grant(admin, wid, limit="1000")
    _grant(admin, sid)

    held = _approve(first, case, "6000.00")  # above the default four-eyes threshold of 5000
    assert held.status_code == 200, held.text
    assert held.json()["status"] == "awaiting_second_approval" and held.json()["four_eyes_required"]
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "HUMAN_REVIEW"
    assert admin.get(f"/claims/{case.claim_id}/decisions").json() == []
    ws = second.get(case.url()).json()
    assert ws["pending_decision"]["approved_amount"] == "6000.0000" and ws["first_decider_name"]

    same = first.post(case.url("second-approval"), json={}, headers=_key())
    assert same.status_code == 403 and "different person" in same.json()["detail"]
    low = weak.post(case.url("second-approval"), json={}, headers=_key())
    assert low.status_code == 403 and "exceeds" in low.json()["detail"]
    assert _approve(second, case, "6000.00").status_code == 409  # a first decision is waiting

    no = {"approve": False, "note": "check the invoice"}
    declined = second.post(case.url("second-approval"), json=no, headers=_key())
    assert declined.status_code == 200 and declined.json()["status"] == "in_progress"
    assert declined.json()["assigned_to"] == fid
    assert _approve(first, case, "6000.00").json()["status"] == "awaiting_second_approval"
    ok = second.post(case.url("second-approval"), json={"note": "agree"}, headers=_key())
    assert ok.status_code == 200 and ok.json()["outcome"] == "approved"
    d = admin.get(f"/claims/{case.claim_id}/decisions").json()[0]
    assert (d["decided_by"], d["second_approver_id"]) == (fid, sid)
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "APPROVED"
    assert await _resume_worker(settings, case, admin) == "completed"


async def test_override_with_reason_is_recorded_and_needs_four_eyes(
    new_client: Factory, settings: Settings, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    await _set(migrator_engine, case.review_id, recommendation="APPROVE_RECOMMENDED", payout="815.00")
    a, aid = _member(new_client, admin, ["reviewer"])
    b, bid = _member(new_client, admin, ["reviewer"])
    _grant(admin, aid, can_reject=True)
    _grant(admin, bid, can_reject=True)
    body = {
        "reason_code": "EXCLUSION",
        "reason": "racing exclusion",
        "override_reason": "telematics show track use",
    }
    rej = a.post(case.url("reject"), json=body, headers=_key())
    assert rej.status_code == 200 and rej.json()["status"] == "awaiting_second_approval", rej.text
    done = b.post(case.url("second-approval"), json={}, headers=_key())
    assert done.status_code == 200 and done.json()["outcome"] == "rejected"
    d = admin.get(f"/claims/{case.claim_id}/decisions").json()[0]
    assert d["override"] and d["override_reason"] == "telematics show track use"
    assert d["reason_code"] == "EXCLUSION" and d["approved_amount"] is None
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "REJECTED"
    assert await _resume_worker(settings, case, admin) == "completed"


async def test_request_information_goes_back_to_the_customer(new_client: Factory, settings: Settings) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    reviewer, _ = _member(new_client, admin, ["reviewer"])
    body = {"items": ["repair invoice"], "message": "Please send the final invoice"}
    r = reviewer.post(case.url("request-information"), json=body, headers=_key())
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "more_information"
    assert await _resume_worker(settings, case, admin) == "interrupted"
    detail = admin.get(f"/workflow-executions/{case.execution_id}").json()
    assert detail["interrupt"]["kind"] == "more_information"
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "AWAITING_INFORMATION"
    d = admin.get(f"/claims/{case.claim_id}/decisions").json()[0]
    assert d["decision"] == "more_information" and d["reason"] == "Please send the final invoice"
    assert reviewer.get("/reviews").json() == []


async def test_escalation_and_return_for_reassessment(new_client: Factory, settings: Settings) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    a, _ = _member(new_client, admin, ["reviewer"])
    b, bid = _member(new_client, admin, ["reviewer"])
    _, xid = _member(new_client, admin, ["claims_adjuster"])

    bad = a.post(case.url("escalate"), json={"to_user_id": xid, "reason": "senior"})
    assert bad.status_code == 422  # the target cannot decide reviews
    esc = a.post(case.url("escalate"), json={"to_user_id": bid, "reason": "needs senior sign-off"})
    assert esc.status_code == 200 and esc.json()["status"] == "escalated"
    assert esc.json()["assigned_to"] == bid
    mine = b.get("/reviews", params={"assignee": "me"}).json()
    assert mine[0]["status"] == "escalated" and mine[0]["assigned_to_name"]
    assert b.post(case.url("claim")).json()["status"] == "in_progress"

    body = {"reason": "recheck depreciation", "instructions": "vehicle is three years old"}
    ret = b.post(case.url("return"), json=body, headers=_key())
    assert ret.status_code == 200 and ret.json()["outcome"] == "returned", ret.text
    assert admin.get(f"/claims/{case.claim_id}").json()["status"] == "HUMAN_REVIEW"
    await _resume_worker(settings, case, admin)
    detail = admin.get(f"/workflow-executions/{case.execution_id}").json()
    assert "CLAIM_REASSESSMENT" in [e["node"] for e in detail["events"]]
    assert detail["reassessment_count"] == 1
    decisions = admin.get(f"/claims/{case.claim_id}/decisions").json()
    assert [d["decision"] for d in decisions] == ["returned"]


async def test_cancelling_processing_cancels_the_open_review(new_client: Factory, settings: Settings) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    cancel = admin.post(f"/claims/{case.claim_id}/processing/cancel")
    assert cancel.status_code in (200, 202), cancel.text
    assert admin.get("/reviews").json() == []
    cancelled = admin.get("/reviews", params={"status": "cancelled"}).json()
    assert [r["id"] for r in cancelled] == [case.review_id]


async def test_authorities_are_managed_by_user_managers_only(new_client: Factory) -> None:
    admin = new_client()
    me = register(admin)
    reviewer, rid = _member(new_client, admin, ["reviewer"])
    item = {"max_approval_amount": "100", "currency": "usd", "valid_from": TODAY.isoformat()}
    assert admin.put(f"/users/{me['user']['id']}/authorities", json=[item]).status_code == 403  # own
    assert reviewer.put(f"/users/{rid}/authorities", json=[item]).status_code == 403
    backwards = {**item, "valid_to": (TODAY - timedelta(days=2)).isoformat()}
    assert admin.put(f"/users/{rid}/authorities", json=[backwards]).status_code == 422
    saved = admin.put(f"/users/{rid}/authorities", json=[item])
    assert saved.status_code == 200 and saved.json()[0]["currency"] == "USD"
    assert reviewer.get(f"/users/{rid}/authorities").json()[0]["max_approval_amount"] == "100.0000"
    assert reviewer.get(f"/users/{me['user']['id']}/authorities").status_code == 404
    assert admin.put(f"/users/{rid}/authorities", json=[]).json() == []
