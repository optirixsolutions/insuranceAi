"""Rule-based document classification (rules_v1).

Each document type has weighted phrases. A type needs at least ``MIN_SCORE`` to be chosen,
and confidence reflects how clearly it beats the runner-up. Anything unclear is left
unclassified for a person to decide; the classifier never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_SCORE = 3.0
PHOTO_MAX_CHARS = 40

RULES: dict[str, list[tuple[str, float]]] = {
    "claim_form": [
        (r"claim form", 4),
        (r"claimant", 1.5),
        (r"declaration", 1.5),
        (r"policy (?:no|number)", 1),
        (r"date of (?:loss|incident|accident)", 1.5),
        (r"signature", 1),
    ],
    "insurance_policy": [
        (r"policy schedule", 4),
        (r"certificate of insurance", 4),
        (r"sum insured", 2),
        (r"period of (?:insurance|cover)", 2),
        (r"exclusions?", 1),
        (r"excess|deductible", 1),
    ],
    "identity_document": [
        (r"passport", 3),
        (r"national identity|identity card|id card", 3),
        (r"date of birth", 1.5),
        (r"nationality", 1.5),
        (r"date of expiry", 1),
    ],
    "driving_licence": [
        (r"driving licen[cs]e|driver'?s licen[cs]e", 4),
        (r"licen[cs]e (?:no|number)", 1.5),
        (r"categor(?:y|ies)", 1),
        (r"date of birth", 0.5),
    ],
    "vehicle_registration": [
        (r"registration certificate|v5c|logbook", 4),
        (r"registered keeper", 2.5),
        (r"vehicle identification number|\bvin\b", 2),
        (r"registration (?:mark|number)", 1),
    ],
    "medical_bill": [
        (r"hospital|clinic|medical centre|medical center", 1.5),
        (r"invoice|bill|statement", 2),
        (r"patient", 1.5),
        (r"consultation|procedure|ward|pharmacy|radiology", 1.5),
        (r"total (?:due|amount|payable)|amount due", 1.5),
    ],
    "hospital_report": [
        (r"discharge summary", 4),
        (r"admission date|date of admission", 2),
        (r"diagnosis", 2),
        (r"attending (?:physician|consultant)", 1.5),
        (r"treatment", 1),
    ],
    "repair_estimate": [
        (r"estimate|quotation|quote", 3),
        (r"labou?r", 1.5),
        (r"parts?", 1),
        (r"paint|panel|bodywork", 1.5),
        (r"registration|vehicle", 1),
        (r"valid for", 1),
    ],
    "repair_invoice": [
        (r"\binvoice\b", 3),
        (r"labou?r", 1.5),
        (r"parts?", 1),
        (r"vat|tax", 0.5),
        (r"paint|panel|bodywork|garage|workshop", 1.5),
        (r"paid|balance due", 1),
    ],
    "accident_report": [
        (r"accident report|incident report", 4),
        (r"other (?:party|driver|vehicle)", 2),
        (r"witness", 1.5),
        (r"sketch|diagram", 1),
        (r"weather|road conditions", 1),
    ],
    "police_report": [
        (r"police", 3),
        (r"crime reference|incident number|occurrence number", 3),
        (r"officer|constable|station", 1.5),
        (r"statement", 1),
    ],
    "bank_information": [
        (r"bank details|account holder", 3),
        (r"sort code|routing number|iban|swift|bic", 2.5),
        (r"account (?:no|number)", 1.5),
    ],
    "supporting_correspondence": [
        (r"dear\b", 2),
        (r"yours sincerely|kind regards|yours faithfully", 2),
        (r"\bre:", 1),
    ],
}
_COMPILED = {k: [(re.compile(p, re.IGNORECASE), w) for p, w in v] for k, v in RULES.items()}
DOCUMENT_TYPES = (*RULES.keys(), "photo_evidence")


@dataclass(frozen=True, slots=True)
class Classification:
    document_type: str | None
    confidence: float
    scores: dict[str, float]


def classify(text: str, *, is_image: bool) -> Classification:
    stripped = text.strip()
    if is_image and len(stripped) < PHOTO_MAX_CHARS:
        # A picture with (almost) no text: almost certainly photographic evidence of the loss.
        return Classification("photo_evidence", 0.8, {"photo_evidence": 1.0})
    scores: dict[str, float] = {}
    for doc_type, rules in _COMPILED.items():
        score = sum(weight for pattern, weight in rules if pattern.search(stripped))
        if score:
            scores[doc_type] = score
    if not scores:
        return Classification(None, 0.0, {})
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < MIN_SCORE:
        return Classification(None, 0.0, scores)
    margin = (best_score - runner_up) / best_score
    confidence = round(min(0.99, 0.5 + 0.5 * margin + min(best_score, 10) / 40), 3)
    return Classification(best, confidence, scores)
