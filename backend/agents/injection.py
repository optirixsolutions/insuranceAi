"""Detection of instruction-like text aimed at automated systems (indicator F-INJECTION).

Detection never changes what the agents do with the text (it is always treated as data); it
raises a fraud indicator and an escalation so a human sees the attempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_GAP = r"[^.\n]{0,40}"
PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "override",
        r"\b(ignore|disregard|forget|override)\b"
        + _GAP
        + r"\b(previous|prior|above|earlier|all|system|your)\b[^.\n]{0,20}"
        r"\b(instructions?|rules?|prompts?|guidelines?|context)\b",
    ),
    (
        "role",
        r"\b(you are now|act as|pretend to be|from now on you)\b[^.\n]{0,60}"
        r"\b(assistant|ai|model|system|adjuster|approver|admin)\b",
    ),
    (
        "system_prompt",
        r"\b(system prompt|developer message)\b|<\s*/?\s*(?:system|instructions?)\s*>|\[\s*inst\s*\]",
    ),
    (
        "directive",
        r"\b(approve|accept|pay|authori[sz]e)\b[^.\n]{0,30}\b(this|the)\s+claim\b[^.\n]{0,30}"
        r"\b(immediately|automatically|in full|without review)\b",
    ),
    (
        "field_set",
        r"\b(set|change|output|return|respond with)\b[^.\n]{0,20}"
        r"\b(claimed_amount|risk_score|recommendation|payout|fraud score|status)\b",
    ),
    (
        "ai_address",
        r"\b(dear|attention|note to|message for)\b[^.\n]{0,10}"
        r"\b(ai|llm|model|bot|automated system|assistant)\b",
    ),
)
_COMPILED = [(name, re.compile(p, re.IGNORECASE)) for name, p in PATTERNS]


@dataclass(frozen=True, slots=True)
class InjectionHit:
    pattern: str
    excerpt: str


def detect(text: str) -> list[InjectionHit]:
    hits = []
    for name, rx in _COMPILED:
        for m in rx.finditer(text):
            start, end = max(0, m.start() - 40), min(len(text), m.end() + 40)
            hits.append(InjectionHit(name, " ".join(text[start:end].split())[:300]))
    return hits
