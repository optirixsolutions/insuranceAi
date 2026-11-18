"""Model gateway on real infrastructure: Redis throttling, PostgreSQL registry, live OpenRouter catalogue."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from model_gateway.throttle import BudgetExceededError, RateLimitWaitExceededError, Throttle
from tests.integration.conftest import ApiClient, accept, invite, register

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = Redis.from_url(settings.redis_url.get_secret_value())
    yield client
    await client.aclose()


async def test_rate_limit_window_is_shared_and_enforced(redis: Redis) -> None:
    throttle = Throttle(redis, prefix=f"claimos:test:{uuid.uuid4().hex}")
    for _ in range(3):
        assert await throttle.acquire("model-x", 3) < 0.5
    with pytest.raises(RateLimitWaitExceededError):
        await throttle.acquire("model-x", 3, max_wait=0.5)
    assert await throttle.acquire("model-y", 3) < 0.5  # limits are per model


async def test_daily_token_budget(redis: Redis) -> None:
    throttle = Throttle(redis, prefix=f"claimos:test:{uuid.uuid4().hex}")
    org = uuid.uuid4()
    assert await throttle.check_budget(org, 1000) == 1000
    await throttle.spend(org, 900)
    assert await throttle.check_budget(org, 1000) == 100
    await throttle.spend(org, 150)
    with pytest.raises(BudgetExceededError):
        await throttle.check_budget(org, 1000)


def test_catalog_sync_agents_and_assignment_rules(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    summary = admin.post("/models/sync")
    assert summary.status_code == 200, summary.text  # live OpenRouter catalogue
    assert summary.json()["catalog_size"] > 50 and summary.json()["added"] >= 5
    models = admin.get("/models").json()
    by_slug = {m["slug"]: m for m in models}
    assert settings.openrouter_default_model in by_slug
    assert all(m["is_free"] or m["slug"] == settings.openrouter_default_model for m in models)
    free = next(m for m in models if m["slug"] != "openrouter/free")
    assert free["data_policy"] == "may_log" and free["probe_status"] == "never"

    agents = {a["agent_key"]: a for a in admin.get("/agents").json()}
    assert set(agents) == {"document", "policy", "fraud", "assessment", "decision"}
    assert agents["document"]["primary_model_id"] == by_slug[settings.openrouter_default_model]["id"]

    # Catalogue claims are not enough: assigning an unprobed model is refused.
    refused = admin.patch(
        "/agents/document",
        json={"primary_model_id": free["id"]},
        headers={"If-Match": f'"{agents["document"]["row_version"]}"'},
    )
    assert refused.status_code == 422 and "capability probe" in refused.json()["detail"]

    # Approving a model for real data needs a documented justification, and is audited.
    approve = admin.patch(
        f"/models/{free['id']}",
        json={"data_policy": "zero_retention"},
        headers={"If-Match": f'"{free["row_version"]}"'},
    )
    assert approve.status_code == 422
    approved = admin.patch(
        f"/models/{free['id']}",
        json={"data_policy": "zero_retention", "data_policy_note": "Provider DPA v3 §4: no retention"},
        headers={"If-Match": f'"{free["row_version"]}"'},
    )
    assert approved.status_code == 200 and approved.json()["data_policy"] == "zero_retention"
    events = admin.get("/audit-logs", params={"action": "ai."}).json()["items"]
    assert {"ai.catalog_synced", "ai.model_updated"} <= {e["action"] for e in events}


def test_ai_admin_is_restricted_and_tenant_scoped(new_client: Callable[[], ApiClient]) -> None:
    admin, other = new_client(), new_client()
    register(admin)
    register(other)
    admin.post("/models/sync")
    model_id = admin.get("/models").json()[0]["id"]
    manager = new_client()
    accept(manager, invite(admin, ["claims_manager"]))
    assert manager.get("/models").status_code == 403
    assert manager.post("/models/sync").status_code == 403
    assert other.get("/models").json() == []  # registry is per organisation
    assert (
        other.patch(
            f"/models/{model_id}", json={"status": "disabled"}, headers={"If-Match": '"1"'}
        ).status_code
        == 404
    )


def test_calls_fail_clearly_without_an_api_key(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    if settings.openrouter_api_key is not None:
        pytest.skip("An API key is configured in this environment")
    admin = new_client()
    register(admin)
    admin.post("/models/sync")
    status = admin.get("/ai/status").json()
    assert status["api_key_configured"] is False
    model_id = admin.get("/models").json()[0]["id"]
    response = admin.post(f"/models/{model_id}/capability-test")
    assert response.status_code == 503 and "OPENROUTER_API_KEY" in response.json()["detail"]
    run = admin.post(
        "/ai/test-runs",
        json={
            "agent_key": "document",
            "confirm_synthetic": True,
            "text": "Synthetic note: rear-ended on 2026-10-08, claim 1,475 GBP.",
        },
    )
    assert run.status_code == 503


async def test_invocation_log_is_append_only(
    app_engine: AsyncEngine, new_client: Callable[[], ApiClient]
) -> None:
    me = register(new_client())
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": me["organization"]["id"]})
        await conn.execute(
            text(
                "INSERT INTO model_invocations (organization_id, agent_key, purpose, model_slug, attempt, "
                "status, latency_ms, request_sha256, data_classification) "
                "VALUES (:o, 'document', 'test', 'm', 1, 'ok', 5, 'x', 'synthetic')"
            ),
            {"o": me["organization"]["id"]},
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text("UPDATE model_invocations SET status = 'forged'"))
