"""Shared request throttling and token budgets (Redis), so every worker respects the same limits.

* Per-model rate limit: a sliding one-minute window. Callers wait for a slot (up to ``max_wait``)
  instead of hammering a provider that will answer 429 anyway.
* Per-organisation daily token budget: requests are refused once the budget is spent.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis


class BudgetExceededError(RuntimeError):
    pass


class RateLimitWaitExceededError(RuntimeError):
    pass


class Throttle:
    def __init__(self, redis: Redis, *, prefix: str = "claimos:ai") -> None:
        self.redis, self.prefix = redis, prefix

    async def acquire(self, model: str, requests_per_minute: int, *, max_wait: float = 30.0) -> float:
        """Reserve a request slot for ``model``; returns seconds waited."""
        key = f"{self.prefix}:rpm:{model}"
        started = time.monotonic()
        while True:
            now = time.time()
            member = f"{now}:{uuid.uuid4().hex}"
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now - 60)
                pipe.zcard(key)
                _, count = await pipe.execute()
            if int(count) < requests_per_minute:
                async with self.redis.pipeline(transaction=True) as pipe:
                    pipe.zadd(key, {member: now})
                    pipe.expire(key, 120)
                    await pipe.execute()
                return time.monotonic() - started
            oldest = await self.redis.zrange(key, 0, 0, withscores=True)
            wait = max(0.05, (float(oldest[0][1]) + 60 - now) if oldest else 0.5)
            if time.monotonic() - started + wait > max_wait:
                raise RateLimitWaitExceededError(f"No request slot for {model} within {max_wait:.0f}s")
            await asyncio.sleep(min(wait, 2.0))

    def _budget_key(self, organization_id: uuid.UUID) -> str:
        return f"{self.prefix}:tokens:{organization_id}:{datetime.now(UTC).date().isoformat()}"

    async def check_budget(self, organization_id: uuid.UUID, daily_budget: int) -> int:
        used = int(await self.redis.get(self._budget_key(organization_id)) or 0)
        if used >= daily_budget:
            raise BudgetExceededError(f"Daily AI token budget of {daily_budget} is spent ({used} used)")
        return daily_budget - used

    async def spend(self, organization_id: uuid.UUID, tokens: int) -> None:
        key = self._budget_key(organization_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, tokens)
            pipe.expire(key, 2 * 86400)
            await pipe.execute()
