"""Brute-force protection for authentication, backed by Redis sliding counters."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class ThrottleDecision:
    allowed: bool
    retry_after_seconds: int = 0


class LoginThrottle:
    """Counts failed logins per account fingerprint and per client IP.

    Once a counter reaches its limit, further attempts are refused (without checking the
    password) until the window expires. Successful logins clear the account counter.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        max_account_failures: int,
        max_ip_failures: int,
        window_seconds: int,
    ) -> None:
        self._redis = redis
        self._max_account = max_account_failures
        self._max_ip = max_ip_failures
        self._window = window_seconds

    @staticmethod
    def _account_key(fingerprint: bytes) -> str:
        return f"claimos:auth:fail:acct:{fingerprint.hex()}"

    @staticmethod
    def _ip_key(ip: str) -> str:
        return f"claimos:auth:fail:ip:{ip}"

    async def check(self, fingerprint: bytes, ip: str | None) -> ThrottleDecision:
        keys = [self._account_key(fingerprint)] + ([self._ip_key(ip)] if ip else [])
        limits = [self._max_account] + ([self._max_ip] if ip else [])
        values = await self._redis.mget(keys)
        for key, value, limit in zip(keys, values, limits, strict=True):
            if value is not None and int(value) >= limit:
                ttl = await self._redis.ttl(key)
                return ThrottleDecision(False, max(int(ttl), 1))
        return ThrottleDecision(True)

    async def record_failure(self, fingerprint: bytes, ip: str | None) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            for key in [self._account_key(fingerprint)] + ([self._ip_key(ip)] if ip else []):
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)
            await pipe.execute()

    async def record_success(self, fingerprint: bytes) -> None:
        await self._redis.delete(self._account_key(fingerprint))
