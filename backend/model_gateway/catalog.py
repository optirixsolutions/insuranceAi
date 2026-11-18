"""OpenRouter model catalogue (public ``GET /models``) parsed into declared capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class CatalogModel:
    slug: str
    name: str
    is_free: bool
    context_length: int | None
    max_completion_tokens: int | None
    input_modalities: list[str]
    declared: dict[str, bool]
    prompt_price_per_mtok: Decimal
    completion_price_per_mtok: Decimal


def _per_million(value: Any) -> Decimal:
    try:
        return (Decimal(str(value or "0")) * 1_000_000).quantize(Decimal("0.000001"))
    except InvalidOperation:
        return Decimal(0)


def parse_catalog(payload: dict[str, Any]) -> list[CatalogModel]:
    models: list[CatalogModel] = []
    for item in payload.get("data", []):
        slug = str(item.get("id", ""))
        if not slug:
            continue
        params = set(item.get("supported_parameters") or [])
        modalities = list((item.get("architecture") or {}).get("input_modalities") or ["text"])
        pricing = item.get("pricing") or {}
        prompt, completion = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        top = item.get("top_provider") or {}
        models.append(
            CatalogModel(
                slug=slug,
                name=str(item.get("name") or slug),
                is_free=prompt == 0 and completion == 0,
                context_length=item.get("context_length"),
                max_completion_tokens=top.get("max_completion_tokens"),
                input_modalities=modalities,
                declared={
                    "json_mode": "response_format" in params,
                    "json_schema": "structured_outputs" in params,
                    "tools": "tools" in params,
                    "vision": "image" in modalities,
                },
                prompt_price_per_mtok=prompt,
                completion_price_per_mtok=completion,
            )
        )
    return models


async def fetch_catalog(base_url: str, request_timeout: float = 30.0) -> list[CatalogModel]:
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        response = await client.get(f"{base_url.rstrip('/')}/models")
        response.raise_for_status()
        return parse_catalog(response.json())
