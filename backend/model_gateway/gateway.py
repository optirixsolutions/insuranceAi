"""The model gateway: the only path from ClaimOS to a language model.

For one structured request it walks the routed model chain. Per model it reserves a rate-limit
slot, retries transient transport failures with exponential backoff and jitter, repairs invalid
JSON a bounded number of times, then falls back to the next model. Every attempt is reported to
an ``InvocationRecorder`` (usage, latency, outcome; never prompt text).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from model_gateway.provider import ChatRequest, ChatResult, Provider, ProviderError
from model_gateway.routing import DataClass, ModelProfile, candidates
from model_gateway.structured import (
    StructuredOutputError,
    instruction,
    parse,
    repair_message,
    response_format,
)
from model_gateway.throttle import BudgetExceededError, RateLimitWaitExceededError, Throttle


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    model_slug: str
    attempt: int
    status: str  # ok | invalid_output | provider_error | rate_limit_wait | budget_exceeded
    error_code: str | None
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    request_sha256: str
    provider_request_id: str | None


InvocationRecorder = Callable[[AttemptRecord], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StructuredCall:
    organization_id: uuid.UUID
    agent_key: str
    purpose: str
    system_prompt: str
    user_content: str | list[dict[str, Any]]
    data_class: DataClass
    required_capabilities: frozenset[str]
    org_allows_real_data: bool
    max_output_tokens: int = 2000
    temperature: float = 0.0
    timeout_seconds: float = 60.0
    max_repairs: int = 2
    # Deterministic post-parse checks (e.g. grounding against the source text). A non-empty
    # list of problems is treated exactly like invalid JSON: repaired, then the next model.
    validate: Callable[[Any], list[str]] | None = None


@dataclass(slots=True)
class StructuredOutcome[M: BaseModel]:
    data: M
    model_slug: str
    attempts: list[AttemptRecord] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


class NoEligibleModelError(RuntimeError):
    pass


class GatewayExhaustedError(RuntimeError):
    def __init__(self, message: str, attempts: list[AttemptRecord]) -> None:
        super().__init__(message)
        self.attempts = attempts


class ModelGateway:
    def __init__(
        self,
        provider: Provider,
        throttle: Throttle | None,
        *,
        daily_token_budget: int,
        max_transport_attempts: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider, self.throttle = provider, throttle
        self.daily_token_budget, self.max_transport_attempts = daily_token_budget, max_transport_attempts
        self.backoff_base, self._sleep = backoff_base, sleep

    async def _send(
        self,
        model: ModelProfile,
        request: ChatRequest,
        call: StructuredCall,
        record: InvocationRecorder,
        attempt_no: int,
        attempts: list[AttemptRecord],
    ) -> ChatResult | None:
        digest = hashlib.sha256(
            json.dumps(request.messages, sort_keys=True, default=str).encode()
        ).hexdigest()
        for transport_try in range(1, self.max_transport_attempts + 1):
            if self.throttle is not None:
                try:
                    await self.throttle.check_budget(call.organization_id, self.daily_token_budget)
                    await self.throttle.acquire(model.slug, model.requests_per_minute)
                except BudgetExceededError:
                    rec = AttemptRecord(
                        model.slug,
                        attempt_no,
                        "budget_exceeded",
                        "budget_exceeded",
                        0,
                        0,
                        0,
                        0.0,
                        digest,
                        None,
                    )
                    attempts.append(rec)
                    await record(rec)
                    raise
                except RateLimitWaitExceededError:
                    rec = AttemptRecord(
                        model.slug,
                        attempt_no,
                        "rate_limit_wait",
                        "local_rate_limit",
                        0,
                        0,
                        0,
                        0.0,
                        digest,
                        None,
                    )
                    attempts.append(rec)
                    await record(rec)
                    return None
            started = time.perf_counter()
            try:
                result = await self.provider.complete(request)
            except ProviderError as exc:
                latency = int((time.perf_counter() - started) * 1000)
                rec = AttemptRecord(
                    model.slug, attempt_no, "provider_error", exc.code, latency, 0, 0, 0.0, digest, None
                )
                attempts.append(rec)
                await record(rec)
                if not exc.retryable or transport_try == self.max_transport_attempts:
                    return None
                delay = exc.retry_after or self.backoff_base * (2 ** (transport_try - 1))
                await self._sleep(min(30.0, delay) * (0.8 + random.random() * 0.4))  # noqa: S311 - jitter only
                continue
            latency = int((time.perf_counter() - started) * 1000)
            cost = (
                result.prompt_tokens * model.prompt_price_per_mtok
                + result.completion_tokens * model.completion_price_per_mtok
            ) / 1_000_000
            if self.throttle is not None:
                await self.throttle.spend(
                    call.organization_id, result.prompt_tokens + result.completion_tokens
                )
            attempts.append(
                AttemptRecord(
                    model.slug,
                    attempt_no,
                    "ok",
                    None,
                    latency,
                    result.prompt_tokens,
                    result.completion_tokens,
                    cost,
                    digest,
                    result.provider_request_id,
                )
            )
            return result
        return None

    async def run_structured[M: BaseModel](
        self, call: StructuredCall, schema: type[M], chain: list[ModelProfile], record: InvocationRecorder
    ) -> StructuredOutcome[M]:
        usable, excluded = candidates(
            chain,
            required=set(call.required_capabilities),
            data_class=call.data_class,
            org_allows_real_data=call.org_allows_real_data,
        )
        if not usable:
            reasons = "; ".join(f"{e.slug}: {e.reason}" for e in excluded) or "no models configured"
            raise NoEligibleModelError(f"No eligible model for '{call.agent_key}' ({reasons})")

        attempts: list[AttemptRecord] = []
        attempt_no = 0
        for model in usable:
            fmt = response_format(schema, set(model.verified))
            system = (
                call.system_prompt
                if fmt and fmt["type"] == "json_schema"
                else (f"{call.system_prompt}\n\n{instruction(schema)}")
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": call.user_content},
            ]
            for _repair in range(call.max_repairs + 1):
                attempt_no += 1
                request = ChatRequest(
                    model=model.slug,
                    messages=messages,
                    max_tokens=call.max_output_tokens,
                    temperature=call.temperature,
                    response_format=fmt,
                    timeout_seconds=call.timeout_seconds,
                )
                result = await self._send(model, request, call, record, attempt_no, attempts)
                if result is None:
                    break  # transport failure on this model: fall back to the next one
                try:
                    data = parse(schema, result.content)
                    problems = call.validate(data) if call.validate else []
                    if problems:
                        raise StructuredOutputError("; ".join(problems), result.content)
                except StructuredOutputError as exc:
                    last = attempts[-1]
                    invalid = AttemptRecord(
                        last.model_slug,
                        last.attempt,
                        "invalid_output",
                        "invalid_output",
                        last.latency_ms,
                        last.prompt_tokens,
                        last.completion_tokens,
                        last.cost_usd,
                        last.request_sha256,
                        last.provider_request_id,
                    )
                    attempts[-1] = invalid
                    await record(invalid)
                    messages = [
                        *messages,
                        {"role": "assistant", "content": result.content[:4000]},
                        repair_message(exc),
                    ]
                    continue
                await record(attempts[-1])
                return StructuredOutcome(
                    data, model.slug, attempts, [f"{e.slug}: {e.reason}" for e in excluded]
                )
        raise GatewayExhaustedError(f"All eligible models failed for '{call.agent_key}'", attempts)
