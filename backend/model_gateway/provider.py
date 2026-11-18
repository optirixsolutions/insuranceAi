"""Chat-completion providers behind one interface.

``OpenRouterProvider`` speaks the OpenAI-compatible API. ``MockProvider`` returns scripted,
deterministic responses for tests of the gateway's own logic (retries, repair, fallback);
it is a development adapter and is refused in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = 1000
    temperature: float = 0.0
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None
    provider_request_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ProviderError(Exception):
    """A failed provider call. ``retryable`` errors may succeed if the same request is repeated."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code, self.retryable, self.status, self.retry_after = code, retryable, status, retry_after


class Provider(Protocol):
    name: str

    async def complete(self, request: ChatRequest) -> ChatResult: ...


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, *, api_key: str, base_url: str, app_url: str, app_title: str = "ClaimOS") -> None:
        # Identify the application generically; never send user or tenant identifiers upstream.
        self._headers = {"Authorization": f"Bearer {api_key}", "HTTP-Referer": app_url, "X-Title": app_title}
        self._base = base_url.rstrip("/")

    async def key_info(self, request_timeout: float = 10.0) -> dict[str, Any]:
        """Validate the key with OpenRouter (``GET /key``: no tokens consumed)."""
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.get(f"{self._base}/key", headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderError("transport", f"Network error ({type(exc).__name__})", retryable=True) from exc
        if response.status_code >= 400:
            self._parse(response)  # raises a classified ProviderError
        data: dict[str, Any] = response.json().get("data") or {}
        return data

    async def complete(self, request: ChatRequest) -> ChatResult:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.tools:
            payload["tools"] = request.tools
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                response = await client.post(
                    f"{self._base}/chat/completions", json=payload, headers=self._headers
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "timeout", "The model provider did not answer in time", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError("transport", f"Network error ({type(exc).__name__})", retryable=True) from exc
        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> ChatResult:
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "invalid_body",
                f"Non-JSON response (HTTP {response.status_code})",
                retryable=response.status_code in RETRYABLE_STATUS,
                status=response.status_code,
            ) from exc
        error = body.get("error") if isinstance(body, dict) else None
        if response.status_code >= 400 or error:
            status = (
                int((error or {}).get("code") or response.status_code)
                if isinstance(error, dict)
                else response.status_code
            )
            message = (
                str((error or {}).get("message", "provider error"))
                if isinstance(error, dict)
                else "provider error"
            )
            retry_after = response.headers.get("retry-after")
            code = {
                401: "unauthorized",
                402: "insufficient_credits",
                403: "forbidden",
                404: "model_not_found",
                429: "rate_limited",
            }.get(status, "provider_error")
            # An account-wide daily quota (e.g. OpenRouter "free-models-per-day") is not a property of
            # the model and will not clear by retrying: surface it as its own, non-retryable code.
            daily = status == 429 and ("per-day" in message or "per day" in message.lower())
            if daily:
                code = "quota_exhausted"
            raise ProviderError(
                code,
                message[:300],
                retryable=status in RETRYABLE_STATUS and not daily,
                status=status,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("empty_response", "The provider returned no choices", retryable=True)
        reply: dict[str, Any] = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        content = reply.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ChatResult(
            content=str(content),
            model=str(body.get("model") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choices[0].get("finish_reason"),
            provider_request_id=body.get("id"),
            tool_calls=list(reply.get("tool_calls") or []),
        )


Script = Callable[[ChatRequest], ChatResult | ProviderError]


class MockProvider:
    """Deterministic scripted provider for tests. Each model slug has a queue of outcomes."""

    name = "mock"

    def __init__(self, scripts: dict[str, list[ChatResult | ProviderError | Script]] | None = None) -> None:
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.calls: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.calls.append(request)
        queue = self.scripts.get(request.model)
        if not queue:
            raise ProviderError(
                "model_not_found", f"No scripted response for {request.model}", retryable=False, status=404
            )
        outcome = queue.pop(0)
        if callable(outcome) and not isinstance(outcome, ChatResult | ProviderError):
            outcome = outcome(request)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome
