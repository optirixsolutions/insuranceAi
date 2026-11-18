"""Capability probes: verify what a model actually does before any agent may rely on it.

Catalogue metadata says what a model *should* support. These probes send small synthetic
requests and check the answers, so routing never depends on unverified claims.
"""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict

from model_gateway.provider import ChatRequest, Provider, ProviderError
from model_gateway.structured import StructuredOutputError, extract_json, parse, strict_schema

PROBE_CODE = "4729"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    capability: str
    ok: bool
    latency_ms: int
    detail: str
    tested_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "detail": self.detail[:300],
            "tested_at": self.tested_at,
        }


class _Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_number: str
    total: float
    currency: str


_TEXT = "Invoice INV-2207 from Bridgewater Auto Body. Total due: 1,475.00 GBP."


def _image_data_url() -> str:
    image = Image.new("RGB", (480, 200), "white")
    ImageDraw.Draw(image).text((60, 50), PROBE_CODE, fill="black", font=ImageFont.load_default(size=96))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


async def _timed(provider: Provider, request: ChatRequest) -> tuple[str, int]:
    started = time.perf_counter()
    result = await provider.complete(request)
    if result.tool_calls:
        return json.dumps(result.tool_calls), int((time.perf_counter() - started) * 1000)
    return result.content, int((time.perf_counter() - started) * 1000)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def probe(
    provider: Provider, model: str, capability: str, request_timeout: float = 60.0
) -> ProbeResult:
    base: dict[str, Any] = {
        "model": model,
        "max_tokens": 300,
        "temperature": 0.0,
        "timeout_seconds": request_timeout,
    }
    try:
        if capability == "json_mode":
            text, ms = await _timed(
                provider,
                ChatRequest(
                    **base,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "user",
                            "content": f"Return JSON with keys invoice_number, total, currency for: {_TEXT}",
                        }
                    ],
                ),
            )
            data = extract_json(text)
            ok = isinstance(data, dict) and "INV-2207" in json.dumps(data)
            return ProbeResult(
                capability,
                ok,
                ms,
                "valid JSON with expected values" if ok else f"unexpected: {text[:120]}",
                _now(),
            )
        if capability == "json_schema":
            fmt = {
                "type": "json_schema",
                "json_schema": {"name": "invoice", "strict": True, "schema": strict_schema(_Invoice)},
            }
            text, ms = await _timed(
                provider,
                ChatRequest(
                    **base,
                    response_format=fmt,
                    messages=[{"role": "user", "content": f"Extract the invoice fields: {_TEXT}"}],
                ),
            )
            parsed = parse(_Invoice, text)
            ok = parsed.invoice_number == "INV-2207" and abs(parsed.total - 1475) < 0.01
            return ProbeResult(
                capability, ok, ms, "schema-valid and correct" if ok else f"wrong values: {parsed}", _now()
            )
        if capability == "tools":
            tool = {
                "type": "function",
                "function": {
                    "name": "record_total",
                    "description": "Record an invoice total",
                    "parameters": {
                        "type": "object",
                        "properties": {"total": {"type": "number"}},
                        "required": ["total"],
                    },
                },
            }
            text, ms = await _timed(
                provider,
                ChatRequest(
                    **base,
                    tools=[tool],
                    messages=[{"role": "user", "content": f"Call record_total with the total from: {_TEXT}"}],
                ),
            )
            ok = "record_total" in text and "1475" in text.replace(",", "")
            return ProbeResult(
                capability, ok, ms, "tool call returned" if ok else "no matching tool call", _now()
            )
        if capability == "vision":
            text, ms = await _timed(
                provider,
                ChatRequest(
                    **base,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "What number is written in this image? Answer with digits only.",
                                },
                                {"type": "image_url", "image_url": {"url": _image_data_url()}},
                            ],
                        }
                    ],
                ),
            )
            ok = PROBE_CODE in text.replace(" ", "")
            return ProbeResult(
                capability, ok, ms, "read the test number" if ok else f"answered: {text[:60]!r}", _now()
            )
        return ProbeResult(capability, False, 0, "unknown capability", _now())
    except StructuredOutputError as exc:
        return ProbeResult(capability, False, 0, f"invalid output: {exc}", _now())
    except ProviderError as exc:
        return ProbeResult(capability, False, 0, f"provider error {exc.code}: {exc}", _now())


CAPABILITIES = ("json_mode", "json_schema", "tools", "vision")
# Account-level failures say nothing about the model; a probe run must stop instead of recording them.
ACCOUNT_ERRORS = ("unauthorized", "forbidden", "insufficient_credits", "quota_exhausted")


def account_error(result: ProbeResult) -> bool:
    return any(result.detail.startswith(f"provider error {code}") for code in ACCOUNT_ERRORS)
