"""Structured (JSON) output: request formatting, tolerant parsing and strict validation.

Model output is untrusted. It is parsed leniently (code fences, leading prose) but validated
strictly against a Pydantic model with ``extra="forbid"``. Invalid output triggers a repair
request that shows the model its own validation errors, within a fixed limit.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class StructuredOutputError(ValueError):
    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def response_format(schema: type[BaseModel], capabilities: set[str]) -> dict[str, Any] | None:
    """Strongest format the model has *verified*: JSON schema, else JSON mode, else prompt-only."""
    if "json_schema" in capabilities:
        return {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_schema(schema)},
        }
    if "json_mode" in capabilities:
        return {"type": "json_object"}
    return None


def strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema with ``additionalProperties: false`` everywhere (required by strict mode)."""
    raw = schema.model_json_schema()

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(raw)
    return result


def instruction(schema: type[BaseModel]) -> str:
    return (
        "Respond with a single JSON object and nothing else (no prose, no code fences). "
        "It must validate against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), separators=(",", ":"))
    )


def extract_json(text: str) -> Any:
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate.startswith(("{", "[")):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise StructuredOutputError("No JSON object found in the model output", text)
        candidate = candidate[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"Invalid JSON: {exc.msg} at position {exc.pos}", text) from exc


def parse[M: BaseModel](schema: type[M], text: str) -> M:
    data = extract_json(text)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()[:8]
        )
        raise StructuredOutputError(f"Schema validation failed: {problems}", text) from exc


def repair_message(error: StructuredOutputError) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            f"Your previous answer was rejected: {error}. Reply again with only the corrected JSON object."
        ),
    }
