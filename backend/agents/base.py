"""Common agent contract: context, model client, prompts and the ``Agent`` base class.

Agents *return* results; the service persists them (``agent_runs`` / ``agent_outputs``). The model
is optional everywhere: every agent has a deterministic core and degrades to it, saying so in
``llm_note``, when no verified model is available or the gateway fails.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from agents.contracts import AgentResult
from agents.snapshot import ClaimSnapshot, ScopedToolbox
from rules_engine.rule_set import DEFAULT_CONFIG, DEFAULT_VERSION, RuleSetConfig

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True, slots=True)
class Prompt:
    agent_key: str
    version: str
    content: str
    sha256: str
    path: str


# Prompt version per agent for the current run (set by the agent service from the organisation's
# configuration, or by an evaluation run for a candidate). Unset agents use v1.
PROMPT_VERSIONS: ContextVar[dict[str, str] | None] = ContextVar("prompt_versions", default=None)
VERSION_PATTERN = re.compile(r"^v[0-9]{1,3}$")


def prompt_versions(agent_key: str) -> list[str]:
    """Versions available as source-controlled files, oldest first."""
    found = [p.name.split(".")[1] for p in PROMPTS_DIR.glob(f"{agent_key}.v*.md")]
    return sorted((v for v in found if VERSION_PATTERN.match(v)), key=lambda v: int(v[1:]))


def load_prompt(agent_key: str, version: str | None = None) -> Prompt:
    version = version or (PROMPT_VERSIONS.get() or {}).get(agent_key, "v1")
    if not VERSION_PATTERN.match(version):
        raise ValueError(f"invalid prompt version {version!r}")
    path = PROMPTS_DIR / f"{agent_key}.{version}.md"
    content = path.read_text(encoding="utf-8")
    return Prompt(
        agent_key,
        version,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        f"prompts/{agent_key}.{version}.md",
    )


class LlmUnavailableError(RuntimeError):
    """No verified model, budget exhausted, or every model failed: use the deterministic core."""


@dataclass(slots=True)
class LlmCall:
    model: str | None
    attempts: list[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LlmClient(Protocol):
    async def structured[M: BaseModel](
        self,
        schema: type[M],
        *,
        system: str,
        user: str,
        validate: Callable[[M], list[str]] | None = None,
    ) -> M: ...


@dataclass(slots=True)
class AgentContext:
    snapshot: ClaimSnapshot
    tools: ScopedToolbox
    llm: LlmClient | None
    prompt: Prompt | None
    llm_calls: list[LlmCall] = field(default_factory=list)

    @property
    def threshold(self) -> float:
        return self.snapshot.confidence_threshold


class Agent(ABC):
    key: ClassVar[str]
    allowed_tools: ClassVar[frozenset[str]]
    uses_llm: ClassVar[bool] = False
    schema_version: ClassVar[str] = "1"

    def context(self, snapshot: ClaimSnapshot, llm: LlmClient | None) -> AgentContext:
        prompt = load_prompt(self.key) if self.uses_llm else None
        return AgentContext(
            snapshot, ScopedToolbox(snapshot, self.allowed_tools), llm if self.uses_llm else None, prompt
        )

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentResult: ...

    async def ask[M: BaseModel](
        self,
        ctx: AgentContext,
        schema: type[M],
        user: str,
        validate: Callable[[M], list[str]] | None = None,
    ) -> M | None:
        """One structured model call with the agent's prompt; None means "use the deterministic core"."""
        if ctx.llm is None or ctx.prompt is None:
            return None
        try:
            return await ctx.llm.structured(schema, system=ctx.prompt.content, user=user, validate=validate)
        except LlmUnavailableError:
            return None


AgentFactory = Callable[[], Agent]
Runner = Callable[[Agent, ClaimSnapshot, LlmClient | None], Awaitable[AgentResult]]


async def run_agent(agent: Agent, snapshot: ClaimSnapshot, llm: LlmClient | None = None) -> AgentResult:
    return await agent.run(agent.context(snapshot, llm))


def evidence_text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def rule_config(snapshot: ClaimSnapshot) -> RuleSetConfig:
    """The product's published rule set, or the built-in defaults."""
    if snapshot.rule_set_config is None:
        return DEFAULT_CONFIG
    return RuleSetConfig.model_validate(snapshot.rule_set_config)


def rule_label(snapshot: ClaimSnapshot) -> str:
    if snapshot.rule_set_id is None or snapshot.rule_set_version is None:
        return DEFAULT_VERSION
    return f"rules_v{snapshot.rule_set_version}:{snapshot.rule_set_id[:8]}"
