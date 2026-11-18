"""Phase 11 on real PostgreSQL, Redis, SeaweedFS S3 and the Mailpit SMTP server.

* Reports: generated from persisted rows; every evidence reference in the JSON is present in the PDF text
  (read back with pypdfium2); versions are immutable, numbered per format, downloads are hash-checked.
* Notifications: in-app centre and email delivered over real SMTP (Mailpit API checked).
* Webhooks: delivered to a local HTTP receiver; the receiver verifies the HMAC signature with the same
  code we publish; tampering and replay fail; failures back off and end dead.
Agent outputs with evidence are inserted with the migrator role (the live system test produces them
with the real agents from real documents).
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pypdfium2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.integrations import webhook_signing
from app.integrations.storage import build_storage
from app.models.agents import AgentOutput, AgentRun
from app.services import notification_service, report_service, webhook_service
from tests.integration.conftest import ApiClient, register
from tests.integration.test_reviews_integration import _grant, _key, _member, _to_review

pytestmark = pytest.mark.integration
Factory = Callable[[], ApiClient]
MAILPIT = "http://127.0.0.1:8025"
PAGE = {"kind": "page", "page_number": 2, "excerpt": "Rear bumper replacement 640.00"}


def _outputs(doc_id: str) -> dict[str, dict[str, Any]]:
    """A fraud indicator citing a page, a covered peril citing clause 2.1, a line item citing a page."""
    page = {**PAGE, "id": doc_id}
    indicator = {
        "rule_code": "F-EST-REUSED",
        "severity": "medium",
        "description": "Estimate number seen on another claim",
        "contribution": 15.0,
        "evidence": [page],
    }
    line = {
        "line_no": 1,
        "description": "Rear bumper",
        "category": "parts",
        "claimed_amount": "640.00",
        "eligible_amount": "640.00",
        "depreciation_pct": "0",
        "status": "eligible",
        "evidence": [page],
    }
    return {
        "policy": {
            "policy_active_on_incident_date": True,
            "holder_match": "match",
            "coverages": [
                {
                    "coverage_code": "own_damage",
                    "status": "covered",
                    "reasons": ["collision"],
                    "clause_ids": ["2.1"],
                }
            ],
            "exclusions_triggered": [],
        },
        "fraud": {
            "risk_score": 40,
            "risk_level": "medium",
            "recommended_action": "review",
            "indicators": [indicator],
        },
        "assessment": {
            "status": "calculated",
            "calculator": "motor_own_damage",
            "rule_set_version": "rules_v1-default",
            "currency": "USD",
            "gross_claimed": "640.00",
            "eligible_payout": "590.00",
            "deductible_applied": "50.00",
            "line_items": [line],
            "calculation_trace": [{"op": "deductible", "output": "590.00", "rule_ref": "policy.deductible"}],
        },
        "decision": {
            "recommendation": "APPROVE_RECOMMENDED",
            "matrix_rule": "M5",
            "explanation": "Covered and calculated.",
            "supporting_evidence": [{"kind": "clause", "id": "2.1"}],
            "contradictory_evidence": [],
        },
    }


async def _agent_outputs(engine: AsyncEngine, org: uuid.UUID, claim_id: uuid.UUID, doc_id: str) -> None:
    async with AsyncSession(engine) as s:
        for key, output in _outputs(doc_id).items():
            now = datetime.now(UTC)
            run = AgentRun(
                id=uuid.uuid4(),
                organization_id=org,
                claim_id=claim_id,
                agent_key=key,
                node=key.upper(),
                attempt=1,
                status="succeeded",
                started_at=now,
                finished_at=now,
                duration_ms=5,
                rule_set_version="rules_v1-default",
                input_hash="0" * 64,
                used_llm=False,
                escalate=False,
                findings=[],
                missing=[],
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=Decimal("0"),
            )
            s.add(run)
            await s.flush()
            s.add(
                AgentOutput(
                    id=uuid.uuid4(),
                    organization_id=org,
                    agent_run_id=run.id,
                    schema_name="Test",
                    schema_version="1",
                    output=output,
                    validation_errors=[],
                    output_hash="0" * 64,
                )
            )
        await s.commit()


async def _generate(settings: Settings, org: uuid.UUID, report: dict[str, Any]) -> str:
    db = Database.from_settings(settings, application_name="claimos-test-reports")
    try:
        return await report_service.generate(db, settings, org, uuid.UUID(report["id"]))
    finally:
        await db.dispose()


async def _dispatch(settings: Settings, fn: Any) -> dict[str, int]:
    db = Database.from_settings(settings, application_name="claimos-test-dispatch")
    try:
        result: dict[str, int] = await fn(db, settings)
        return result
    finally:
        await db.dispose()


def _pdf_text(data: bytes) -> str:
    pdf = pypdfium2.PdfDocument(data)
    return "\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))


async def _bound(s: AsyncSession, org: uuid.UUID) -> None:
    await s.execute(text("SELECT set_config('app.org_id', :o, false)"), {"o": str(org)})


class Receiver:
    """A real HTTP endpoint on 127.0.0.1 that records what it receives and answers ``status``."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.status = 200
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks/claimos"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    r = Receiver()
    yield r
    r.close()


# --- reports -------------------------------------------------------------------------------------


async def test_report_cites_evidence_in_json_and_pdf(
    new_client: Factory, settings: Settings, migrator_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    reviewer, rid = _member(new_client, admin, ["reviewer"])
    _grant(admin, rid)
    body = {"approved_amount": "590.00", "reason": "verified"}
    assert reviewer.post(case.url("approve"), json=body, headers=_key()).status_code == 200
    await _agent_outputs(migrator_engine, case.org, uuid.UUID(case.claim_id), str(uuid.uuid4()))

    url = f"/claims/{case.claim_id}/reports"
    key = {"Idempotency-Key": uuid.uuid4().hex}
    pdf = admin.post(url, json={"format": "pdf"}, headers=key)
    assert pdf.status_code == 202 and pdf.json()["status"] == "queued" and pdf.json()["version_no"] == 1, (
        pdf.text
    )
    replay = admin.post(url, json={"format": "pdf"}, headers=key)
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert admin.post(url, json={"format": "pdf"}, headers=_key()).status_code == 409  # one queued per format
    js = admin.post(url, json={"format": "json"}, headers=_key()).json()
    assert await _generate(settings, case.org, pdf.json()) == "ready"
    assert await _generate(settings, case.org, js) == "ready"

    content = admin.get(f"/reports/{js['id']}/download")
    assert content.status_code == 200 and content.headers["content-type"].startswith("application/json")
    report = content.json()
    evidence = {e["ref"]: e for e in report["evidence"]}
    assert {"page", "clause"} <= {e["kind"] for e in report["evidence"]}
    indicator = report["fraud"]["indicators"][0]
    assert indicator["code"] == "F-EST-REUSED" and evidence[indicator["evidence"][0]]["page"] == 2
    assert evidence[report["coverage"]["coverages"][0]["evidence"][0]]["label"].startswith("Clause 2.1")
    assert report["assessment"]["line_items"][0]["evidence"] == indicator["evidence"]  # same page, same label
    decision = report["decisions"][-1]
    assert (decision["decision"], decision["mode"], decision["reason"]) == ("approved", "human", "verified")
    assert report["report"]["synthetic"] is True and report["recommendation"]["evidence"]

    pdf_dl = admin.get(f"/reports/{pdf.json()['id']}/download")
    assert pdf_dl.status_code == 200 and pdf_dl.content[:5] == b"%PDF-"
    versions = admin.get(url).json()
    recorded = next(v for v in versions if v["id"] == pdf.json()["id"])
    assert pdf_dl.headers["x-content-sha256"] == recorded["sha256"]
    pdf_text = _pdf_text(pdf_dl.content)
    assert report["claim"]["reference"] in pdf_text and "Evidence index" in pdf_text
    for ref in evidence:  # every evidence label of the JSON is printed in the PDF
        assert ref in pdf_text, ref
    assert "F-EST-REUSED" in pdf_text and "Clause 2.1" in pdf_text

    assert {(v["format"], v["version_no"], v["status"]) for v in versions} == {
        ("pdf", 1, "ready"),
        ("json", 1, "ready"),
    }
    assert all(v["evidence_count"] == len(evidence) for v in versions)
    assert admin.post(url, json={"format": "pdf"}, headers=_key()).json()["version_no"] == 2

    # immutable once ready, and never deleted by the app role
    for statement, match in (
        ("UPDATE report_versions SET sha256 = 'x' WHERE id = :i", "immutable"),
        ("DELETE FROM report_versions WHERE id = :i", ""),
    ):
        async with app_engine.begin() as c:
            await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(case.org)})
            with pytest.raises(DBAPIError, match=match):
                await c.execute(text(statement), {"i": pdf.json()["id"]})

    # a changed stored object is not served
    stored_key = f"{case.org}/{case.claim_id}/report-v1.pdf"
    await build_storage(settings).put_bytes(
        settings.s3_bucket_reports, stored_key, b"%PDF-x", "application/pdf"
    )
    assert admin.get(f"/reports/{pdf.json()['id']}/download").status_code == 409

    other = new_client()
    register(other)
    assert other.get(f"/reports/{js['id']}/download").status_code == 404
    assert other.get(url).status_code == 404
    assert [r["claim_id"] for r in admin.get("/reports").json()].count(case.claim_id) == 3
    assert any(n["template_key"] == "report.ready" for n in admin.get("/notifications").json()["items"])


# --- notifications -------------------------------------------------------------------------------


async def test_notifications_in_app_and_email_over_smtp(new_client: Factory, settings: Settings) -> None:
    admin = new_client()
    register(admin)
    a, _ = _member(new_client, admin, ["reviewer"])
    b, bid = _member(new_client, admin, ["reviewer"])
    case = await _to_review(admin, settings)
    b_email = b.get("/auth/me").json()["user"]["email"]

    opened = b.get("/notifications").json()
    assert opened["unread"] >= 1 and opened["items"][0]["template_key"] == "review.opened"
    esc = a.post(case.url("escalate"), json={"to_user_id": bid, "reason": "needs senior sign-off"})
    assert esc.status_code == 200, esc.text
    inbox = b.get("/notifications", params={"unread": "true"}).json()
    escalated = next(n for n in inbox["items"] if n["template_key"] == "review.escalated")
    assert escalated["title"].endswith("to you")
    assert escalated["link"] == f"/reviews/{case.claim_id}/{case.review_id}"

    sent = await _dispatch(settings, notification_service.dispatch_pending)
    assert sent.get("sent", 0) >= 1, sent
    async with httpx.AsyncClient(base_url=MAILPIT, timeout=10) as mailpit:
        found = (await mailpit.get("/api/v1/search", params={"query": f"to:{b_email}"})).json()
        assert found["messages_count"] >= 1
        message = (await mailpit.get(f"/api/v1/message/{found['messages'][0]['ID']}")).json()
    assert "escalated" in message["Subject"] and case.claim_id in message["Text"]

    read = b.post(f"/notifications/{escalated['id']}/read")
    assert read.status_code == 200 and read.json()["read_at"]
    assert a.post(f"/notifications/{escalated['id']}/read").status_code == 404  # not yours
    b.post("/notifications/read-all")
    assert b.get("/notifications").json()["unread"] == 0

    sms = admin.post("/integrations/test-message", json={"channel": "sms", "to": "+44 7700 900123"}).json()
    assert (sms["status"], sms["provider"]) == ("sent", "log")
    mail = admin.post("/integrations/test-message", json={"channel": "email"})
    assert (mail.json()["status"], mail.json()["provider"]) == ("sent", "smtp"), mail.text
    assert a.post("/integrations/test-message", json={"channel": "email"}).status_code == 403
    assert admin.get("/integrations/status").json()["sms"] == {
        "provider": "log",
        "sends_real_messages": False,
    }


# --- webhooks ------------------------------------------------------------------------------------


async def test_webhooks_are_signed_verified_and_retried(
    new_client: Factory, settings: Settings, receiver: Receiver
) -> None:
    admin = new_client()
    register(admin)
    plain = {"url": "http://example.com/x", "event_types": ["claim.decided"]}
    assert admin.post("/webhooks", json=plain).status_code == 422  # http only for allow-listed hosts
    assert (
        admin.post("/webhooks", json={"url": receiver.url, "event_types": ["claim.exploded"]}).status_code
        == 422
    )
    events = ["claim.decided", "claim.status_changed"]
    created = admin.post(
        "/webhooks", json={"url": receiver.url, "event_types": events, "description": "test"}
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    secret = sub["secret"]
    assert secret.startswith("whsec_") and sub["secret_hint"] == secret[:10]
    assert "secret" not in admin.get("/webhooks").json()[0]

    case = await _to_review(admin, settings)
    reviewer, rid = _member(new_client, admin, ["reviewer"])
    _grant(admin, rid)
    body = {"approved_amount": "500.00", "reason": "fine"}
    assert reviewer.post(case.url("approve"), json=body, headers=_key()).status_code == 200
    counts = await _dispatch(settings, webhook_service.dispatch_due)
    assert counts.get("delivered", 0) >= 2, counts
    types = [json.loads(raw)["type"] for _, raw in receiver.requests]
    assert "claim.decided" in types and "claim.status_changed" in types

    headers, raw = next((h, r) for h, r in receiver.requests if json.loads(r)["type"] == "claim.decided")
    signature = headers["x-claimos-signature"]
    assert webhook_signing.verify(secret, signature, raw)
    assert not webhook_signing.verify(secret, signature, raw.replace(b"500.00", b"999.00"))  # tampered
    assert not webhook_signing.verify("whsec_wrong", signature, raw)
    assert not webhook_signing.verify(secret, signature, raw, now=int(time.time()) + 3600)  # replayed later
    event = json.loads(raw)
    assert event["data"]["decision"] == "approved" and event["data"]["approved_amount"] == "500.00"
    assert "customer" not in raw.decode() and headers["x-claimos-event"] == "claim.decided"
    deliveries = admin.get(f"/webhooks/{sub['id']}/deliveries").json()
    assert deliveries and all(d["status"] == "delivered" and d["response_code"] == 200 for d in deliveries)

    # failures back off, then the delivery is dead
    receiver.status = 500
    ping = admin.post(f"/webhooks/{sub['id']}/ping")
    assert ping.status_code == 202
    ping_id = uuid.UUID(ping.json()["id"])
    few = settings.model_copy(update={"webhook_max_attempts": 2})
    db = Database.from_settings(few, application_name="claimos-test-retry")
    try:
        async with db.sessionmaker() as s:
            await _bound(s, case.org)
            assert await webhook_service.deliver(s, few, ping_id) == "failed"
            await s.commit()
            row = (
                await s.execute(
                    text("SELECT attempt, next_attempt_at FROM webhook_deliveries WHERE id = :i"),
                    {"i": ping_id},
                )
            ).one()
            assert row[0] == 1 and timedelta(seconds=25) < row[1] - datetime.now(UTC) <= timedelta(seconds=30)
            assert await webhook_service.deliver(s, few, ping_id) == "dead"
            await s.commit()
    finally:
        await db.dispose()
    rows = admin.get(f"/webhooks/{sub['id']}/deliveries").json()
    pinged = next(d for d in rows if d["event_type"] == "webhook.ping")
    assert (pinged["status"], pinged["attempt"], pinged["response_code"]) == ("dead", 2, 500)

    assert admin.delete(f"/webhooks/{sub['id']}").status_code == 204
    assert admin.get("/webhooks").json()[0]["active"] is False
    other = new_client()
    register(other)
    assert other.get(f"/webhooks/{sub['id']}/deliveries").status_code == 404


async def test_webhook_url_must_resolve_to_a_public_address(new_client: Factory, settings: Settings) -> None:
    admin = new_client()
    register(admin)
    created = admin.post("/webhooks", json={"url": "https://10.0.0.5/hook", "event_types": ["claim.decided"]})
    assert created.status_code == 201
    ping = admin.post(f"/webhooks/{created.json()['id']}/ping").json()
    org = uuid.UUID(admin.get("/auth/me").json()["organization"]["id"])
    db = Database.from_settings(settings, application_name="claimos-test-ssrf")
    try:
        async with db.sessionmaker() as s:
            await _bound(s, org)
            assert await webhook_service.deliver(s, settings, uuid.UUID(ping["id"])) == "failed"
            await s.commit()
    finally:
        await db.dispose()
    d = admin.get(f"/webhooks/{created.json()['id']}/deliveries").json()[0]
    assert "non-public address" in d["last_error"] and d["response_code"] is None
