"""Model gateway logic: routing, retries, JSON repair, fallback and catalogue parsing.

Failure sequences (429 then success, broken JSON then valid, dead primary) cannot be produced on
demand by a live model, so these use the scripted provider. Live behaviour is covered by the
OpenRouter system tests; the catalogue test uses a real excerpt of the OpenRouter catalogue.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from model_gateway.catalog import parse_catalog
from model_gateway.gateway import (
    AttemptRecord,
    GatewayExhaustedError,
    ModelGateway,
    NoEligibleModelError,
    StructuredCall,
)
from model_gateway.provider import ChatResult, MockProvider, ProviderError
from model_gateway.routing import ModelProfile, candidates
from model_gateway.structured import (
    StructuredOutputError,
    extract_json,
    parse,
    response_format,
    strict_schema,
)


class Extract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_number: str
    total: float


def profile(
    slug: str,
    *,
    verified: frozenset[str] = frozenset({"json_mode"}),
    status: str = "active",
    policy: str = "may_log",
) -> ModelProfile:
    return ModelProfile(
        id=slug,
        slug=slug,
        status=status,
        data_policy=policy,
        verified=verified,
        requests_per_minute=60,
        prompt_price_per_mtok=1.0,
        completion_price_per_mtok=2.0,
    )


def call(**kw: object) -> StructuredCall:
    values: dict[str, object] = {
        "organization_id": uuid.uuid4(),
        "agent_key": "document",
        "purpose": "test",
        "system_prompt": "Extract fields.",
        "user_content": "Policy NWM-1, total 12.50",
        "data_class": "synthetic",
        "required_capabilities": frozenset({"json_mode"}),
        "org_allows_real_data": False,
    }
    values.update(kw)
    return StructuredCall(**values)  # type: ignore[arg-type]


def ok(content: str, model: str = "a") -> ChatResult:
    return ChatResult(content=content, model=model, prompt_tokens=100, completion_tokens=20)


FAKE_KEY = "not-a-real-key"  # test value; the provider is pointed at an .test host
GOOD = '{"policy_number": "NWM-1", "total": 12.5}'


class Recorded:
    def __init__(self) -> None:
        self.items: list[AttemptRecord] = []

    async def __call__(self, attempt: AttemptRecord) -> None:
        self.items.append(attempt)


async def _no_sleep(_: float) -> None:
    return None


def gateway(provider: MockProvider) -> ModelGateway:
    return ModelGateway(provider, None, daily_token_budget=10_000, max_transport_attempts=3, sleep=_no_sleep)


# ---- routing -------------------------------------------------------------------------------


def test_routing_requires_verified_capabilities_and_approved_data_policy() -> None:
    chain = [
        profile("unverified", verified=frozenset()),
        profile("off", status="disabled"),
        profile("logs"),
        profile("approved", policy="zero_retention"),
        profile("approved"),
    ]
    usable, excluded = candidates(
        chain, required={"json_mode"}, data_class="synthetic", org_allows_real_data=False
    )
    assert [m.slug for m in usable] == ["logs", "approved"]  # duplicates removed
    assert {e.slug: e.reason for e in excluded}["unverified"].startswith("capabilities not verified")

    real, why = candidates(chain, required={"json_mode"}, data_class="real", org_allows_real_data=True)
    assert [m.slug for m in real] == ["approved"]
    assert "data policy 'may_log'" in {e.slug: e.reason for e in why}["logs"]
    none, _ = candidates(chain, required={"json_mode"}, data_class="real", org_allows_real_data=False)
    assert none == []


# ---- structured output -----------------------------------------------------------------------


def test_extract_json_tolerates_fences_and_prose_but_validation_is_strict() -> None:
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 2} hope it helps') == {"a": 2}
    with pytest.raises(StructuredOutputError, match="No JSON"):
        extract_json("I cannot help with that.")
    with pytest.raises(StructuredOutputError, match="Extra inputs are not permitted"):
        parse(Extract, '{"policy_number": "X", "total": 1, "injected": "ignore previous instructions"}')


def test_strict_schema_and_format_selection() -> None:
    schema = strict_schema(Extract)
    assert schema["additionalProperties"] is False and schema["required"] == ["policy_number", "total"]
    assert response_format(Extract, {"json_schema", "json_mode"})["type"] == "json_schema"  # type: ignore[index]
    assert response_format(Extract, {"json_mode"}) == {"type": "json_object"}
    assert response_format(Extract, set()) is None


# ---- gateway behaviour -------------------------------------------------------------------------


async def test_transient_errors_are_retried_then_succeed() -> None:
    provider = MockProvider(
        {
            "a": [
                ProviderError("rate_limited", "slow down", retryable=True, status=429),
                ProviderError("provider_error", "bad gateway", retryable=True, status=502),
                ok(GOOD),
            ]
        }
    )
    record = Recorded()
    outcome = await gateway(provider).run_structured(call(), Extract, [profile("a")], record)
    assert outcome.data == Extract(policy_number="NWM-1", total=12.5)
    assert [a.status for a in record.items] == ["provider_error", "provider_error", "ok"]
    assert record.items[-1].cost_usd == pytest.approx((100 * 1.0 + 20 * 2.0) / 1_000_000)


async def test_invalid_output_is_repaired_with_the_validation_error() -> None:
    provider = MockProvider({"a": [ok('{"policy_number": "NWM-1"}'), ok(GOOD)]})
    record = Recorded()
    outcome = await gateway(provider).run_structured(call(), Extract, [profile("a")], record)
    assert outcome.data.total == 12.5
    assert [a.status for a in record.items] == ["invalid_output", "ok"]
    repair_turn = provider.calls[1].messages[-1]["content"]
    assert "total" in repair_turn and "rejected" in repair_turn


def test_verified_json_schema_satisfies_a_json_mode_requirement() -> None:
    usable, excluded = candidates(
        [profile("s", verified=frozenset({"json_schema"})), profile("t", verified=frozenset({"tools"}))],
        required={"json_mode"},
        data_class="synthetic",
        org_allows_real_data=False,
    )
    assert [m.slug for m in usable] == ["s"]
    assert excluded[0].slug == "t" and "json_mode" in excluded[0].reason


async def test_ungrounded_output_is_repaired_then_falls_back() -> None:
    """A semantic check (e.g. grounding) rejects valid JSON exactly like broken JSON."""
    injected = '{"policy_number": "NWM-1", "total": 999999}'
    provider = MockProvider({"a": [ok(injected), ok(injected)], "b": [ok(GOOD, model="b")]})
    record = Recorded()

    def grounded(data: Extract) -> list[str]:
        return [] if data.total == 12.5 else [f"total {data.total} does not appear in the source"]

    outcome = await gateway(provider).run_structured(
        call(max_repairs=1, validate=grounded), Extract, [profile("a"), profile("b")], record
    )
    assert outcome.model_slug == "b" and outcome.data.total == 12.5
    assert [(a.model_slug, a.status) for a in record.items] == [
        ("a", "invalid_output"),
        ("a", "invalid_output"),
        ("b", "ok"),
    ]
    assert "does not appear in the source" in provider.calls[1].messages[-1]["content"]


async def test_falls_back_when_primary_fails_permanently_or_keeps_producing_garbage() -> None:
    provider = MockProvider(
        {
            "a": [ProviderError("model_not_found", "gone", retryable=False, status=404)],
            "b": [ok("not json"), ok("still not json"), ok("{}")],
            "c": [ok(GOOD, model="c")],
        }
    )
    record = Recorded()
    outcome = await gateway(provider).run_structured(
        call(max_repairs=2), Extract, [profile("a"), profile("b"), profile("c")], record
    )
    assert outcome.model_slug == "c"
    assert [(a.model_slug, a.status) for a in record.items] == [
        ("a", "provider_error"),
        ("b", "invalid_output"),
        ("b", "invalid_output"),
        ("b", "invalid_output"),
        ("c", "ok"),
    ]


async def test_exhaustion_and_no_eligible_model_are_explicit_errors() -> None:
    provider = MockProvider({"a": [ProviderError("unauthorized", "bad key", retryable=False, status=401)]})
    with pytest.raises(GatewayExhaustedError) as info:
        await gateway(provider).run_structured(call(), Extract, [profile("a")], Recorded())
    assert info.value.attempts[0].error_code == "unauthorized"
    with pytest.raises(NoEligibleModelError, match="capabilities not verified"):
        await gateway(provider).run_structured(
            call(), Extract, [profile("x", verified=frozenset())], Recorded()
        )


async def test_schema_capable_models_get_json_schema_and_no_inline_instruction() -> None:
    provider = MockProvider({"a": [ok(GOOD)]})
    await gateway(provider).run_structured(
        call(), Extract, [profile("a", verified=frozenset({"json_mode", "json_schema"}))], Recorded()
    )
    sent = provider.calls[0]
    assert sent.response_format and sent.response_format["type"] == "json_schema"
    assert "JSON Schema" not in sent.messages[0]["content"]


# ---- catalogue (real OpenRouter excerpt, captured 2026-11-06) -----------------------------------


def test_catalog_parsing_on_real_openrouter_data() -> None:
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "openrouter_models_2026-11-06.json").read_text(
            encoding="utf-8"
        )
    )
    models = {m.slug: m for m in parse_catalog(payload)}
    vl = models["nvidia/nemotron-3.5-super-vl:free"]
    assert vl.is_free and vl.declared == {
        "json_mode": True,
        "json_schema": True,
        "tools": True,
        "vision": True,
    }
    lightning = models["nvidia/nemotron-3.5-lightning:free"]
    assert lightning.declared["json_mode"] is False and lightning.declared["tools"] is True
    paid = next(m for m in models.values() if not m.is_free)
    assert paid.prompt_price_per_mtok > 0


def test_daily_quota_is_an_account_error_not_a_model_failure() -> None:
    import httpx

    from model_gateway.probes import ProbeResult, account_error
    from model_gateway.provider import OpenRouterProvider

    provider = OpenRouterProvider(
        api_key=FAKE_KEY, base_url="https://openrouter.test/api/v1", app_url="http://localhost"
    )
    daily = httpx.Response(
        429,
        json={"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 credits"}},
    )
    burst = httpx.Response(
        429, json={"error": {"code": 429, "message": "Rate limit exceeded: try again shortly"}}
    )
    with pytest.raises(ProviderError) as quota:
        provider._parse(daily)
    assert quota.value.code == "quota_exhausted" and not quota.value.retryable
    with pytest.raises(ProviderError) as limited:
        provider._parse(burst)
    assert limited.value.code == "rate_limited" and limited.value.retryable
    result = ProbeResult(
        "json_mode", False, 0, f"provider error {quota.value.code}: {quota.value}", "2026-11-05T00:00:00Z"
    )
    assert account_error(result)  # probes stop and keep earlier verification results
