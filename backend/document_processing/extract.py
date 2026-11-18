"""Rule-based field extraction with evidence (rules_v1).

Every value comes from a concrete line of page text: the excerpt is that line, the value is
a substring of it (grounded by construction) and the bounding box comes from the words that
spell the value. Bank account numbers are intentionally not extracted (data minimisation).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from document_processing.ocr import OcrWord

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
DATE = (
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})"
)
_CODES = r"(?:USD|GBP|EUR|PKR|AED|INR|CAD|AUD)"
AMOUNT = (
    rf"((?:{_CODES[3:-1]}|US\$|\$|£|€|Rs\.?)\s?[0-9][0-9,]*(?:\.[0-9]{{1,2}})?"
    rf"|[0-9][0-9,]*\.[0-9]{{2}}\s?{_CODES}?)"
)
NAME = r"([A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+){1,3})"
CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR", "Rs": "PKR", "Rs.": "PKR"}


@dataclass(frozen=True, slots=True)
class FieldRule:
    name: str
    value_type: str  # string | date | money | identifier
    pattern: re.Pattern[str]
    base_confidence: float


def _rule(name: str, value_type: str, pattern: str, confidence: float) -> FieldRule:
    return FieldRule(name, value_type, re.compile(pattern, re.IGNORECASE), confidence)


RULES: tuple[FieldRule, ...] = (
    _rule(
        "policy_number",
        "identifier",
        r"\bpolicy\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{4,30})",
        0.93,
    ),
    _rule("claim_reference", "identifier", r"\b(CLM-\d{4}-\d{6})\b", 0.95),
    _rule(
        "invoice_number",
        "identifier",
        r"\binvoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,30})",
        0.92,
    ),
    _rule(
        "estimate_number",
        "identifier",
        r"\b(?:estimate|quotation|quote)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,30})",
        0.9,
    ),
    _rule(
        "crime_reference",
        "identifier",
        r"\b(?:crime|police|incident|occurrence)\s*(?:reference|ref\.?|number|no\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{3,30})",
        0.9,
    ),
    _rule(
        "vehicle_registration",
        "identifier",
        r"\bregistration\s*(?:no\.?|number|mark)?\s*[:\-]\s*([A-Z0-9]{2,4}\s?[A-Z0-9]{3,4})",
        0.9,
    ),
    _rule(
        "incident_date",
        "date",
        rf"\b(?:date of (?:loss|incident|accident)|incident date|accident date)\s*[:\-]?\s*{DATE}",
        0.92,
    ),
    _rule("invoice_date", "date", rf"\b(?:invoice date|date of issue|issue date)\s*[:\-]?\s*{DATE}", 0.9),
    _rule(
        "admission_date",
        "date",
        rf"\b(?:admission date|date of admission|admitted(?: on)?)\s*[:\-]?\s*{DATE}",
        0.9,
    ),
    _rule(
        "discharge_date",
        "date",
        rf"\b(?:discharge date|date of discharge|discharged(?: on)?)\s*[:\-]?\s*{DATE}",
        0.9,
    ),
    _rule("date_of_birth", "date", rf"\b(?:date of birth|d\.?o\.?b\.?)\s*[:\-]?\s*{DATE}", 0.9),
    _rule(
        "licence_expiry",
        "date",
        rf"\b(?:licen[cs]e (?:expiry|expires|valid until)|expiry date|valid until)\s*[:\-]?\s*{DATE}",
        0.9,
    ),
    _rule(
        "vehicle_year",
        "string",
        r"\b(?:year of manufacture|vehicle year|model year|first registered)\s*[:\-]?\s*((?:19|20)\d{2})\b",
        0.9,
    ),
    _rule(
        "total_amount",
        "money",
        rf"\b(?:grand total|total (?:due|amount|payable|incl\.? vat)|amount due|balance due|total)"
        rf"\s*[:\-]?\s*{AMOUNT}",
        0.9,
    ),
    _rule(
        "policyholder_name",
        "string",
        rf"\b(?:policyholder|insured|claimant)(?: name)?\s*[:\-]\s*{NAME}",
        0.88,
    ),
    _rule("patient_name", "string", rf"\bpatient(?: name)?\s*[:\-]\s*{NAME}", 0.88),
    _rule(
        "provider_name",
        "string",
        rf"\b(?:garage|repairer|hospital|clinic|provider)(?: name)?\s*[:\-]\s*{NAME}",
        0.85,
    ),
    _rule("diagnosis", "string", r"\bdiagnosis\s*[:\-]\s*([^\n]{3,120})", 0.85),
)


@dataclass(frozen=True, slots=True)
class FieldCandidate:
    field_name: str
    value_text: str
    value_normalized: Any
    value_type: str
    confidence: float
    page_number: int
    evidence_excerpt: str
    bbox: list[float] | None


def normalise_date(raw: str, *, day_first: bool = True) -> tuple[str | None, bool]:
    """Return (ISO date or None, ambiguous). Numeric d/m vs m/d is ambiguous when both are <= 12."""
    value = raw.strip().rstrip(".,")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat(), False
        numeric = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", value)
        if numeric:
            a, b, year = int(numeric[1]), int(numeric[2]), int(numeric[3])
            year += 2000 if year < 100 else 0
            day, month = (a, b) if day_first else (b, a)
            return date(year, month, day).isoformat(), a <= 12 and b <= 12 and a != b
        words = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})", value)
        if words:
            return date(int(words[3]), MONTHS[words[2][:3].lower()], int(words[1])).isoformat(), False
        words = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", value)
        if words:
            return date(int(words[3]), MONTHS[words[1][:3].lower()], int(words[2])).isoformat(), False
    except (ValueError, KeyError):
        return None, False
    return None, False


def normalise_money(raw: str) -> dict[str, str | None] | None:
    match = re.match(
        r"^\s*(USD|GBP|EUR|PKR|AED|INR|CAD|AUD|US\$|\$|£|€|Rs\.?)?\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
        r"\s?(USD|GBP|EUR|PKR|AED|INR|CAD|AUD)?\s*$",
        raw,
    )
    if not match:
        return None
    symbol, number, code = match[1], match[2], match[3]
    try:
        amount = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    currency = code or (CURRENCY_SYMBOLS.get(symbol, symbol) if symbol else None)
    return {"amount": f"{amount:.2f}", "currency": currency}


def _clean(token: str) -> str:
    return re.sub(r"[^\w]", "", token).lower()


def locate(value: str, words: list[OcrWord]) -> list[float] | None:
    """Bounding box of the consecutive words that spell ``value`` (normalised page coordinates)."""
    target = [t for t in (_clean(p) for p in value.split()) if t]
    if not target or not words:
        return None
    cleaned = [_clean(w.text) for w in words]
    for start in range(len(words)):
        if cleaned[start] == target[0] or (len(target) == 1 and target[0] in cleaned[start]):
            end = start + len(target)
            if [c for c in cleaned[start:end]] == target or len(target) == 1:
                boxes = [w.box for w in words[start : max(end, start + 1)]]
                return [
                    round(min(b[0] for b in boxes), 4),
                    round(min(b[1] for b in boxes), 4),
                    round(max(b[2] for b in boxes), 4),
                    round(max(b[3] for b in boxes), 4),
                ]
    return None


def _normaliser(rule: FieldRule) -> Callable[[str], tuple[Any, float]]:
    def run(raw: str) -> tuple[Any, float]:
        if rule.value_type == "date":
            iso, ambiguous = normalise_date(raw)
            return iso, (0.0 if iso is None else (0.8 if ambiguous else 1.0))
        if rule.value_type == "money":
            money = normalise_money(raw)
            return money, (0.0 if money is None else (1.0 if money["currency"] else 0.85))
        if rule.value_type == "identifier":
            return raw.upper().replace(" ", "") if rule.name == "vehicle_registration" else raw.upper(), 1.0
        return " ".join(raw.split()), 1.0

    return run


def extract_fields(pages: list[tuple[int, str, list[OcrWord], float | None]]) -> list[FieldCandidate]:
    """``pages``: (page_number, text, words, ocr_confidence or None for text layers)."""
    best: dict[str, FieldCandidate] = {}
    for page_number, text, words, ocr_conf in pages:
        page_factor = 1.0 if ocr_conf is None else max(0.3, min(1.0, ocr_conf))
        for line in text.splitlines():
            for rule in RULES:
                match = rule.pattern.search(line)
                if not match:
                    continue
                raw = match.group(1).strip().rstrip(".,;")
                normalised, factor = _normaliser(rule)(raw)
                if factor == 0.0:
                    continue  # recognisable label but unreadable value: leave it for a person
                confidence = round(rule.base_confidence * factor * page_factor, 3)
                candidate = FieldCandidate(
                    rule.name,
                    raw,
                    normalised,
                    rule.value_type,
                    confidence,
                    page_number,
                    line.strip()[:500],
                    locate(raw, words),
                )
                if rule.name not in best or confidence > best[rule.name].confidence:
                    best[rule.name] = candidate
    return sorted(best.values(), key=lambda c: (c.page_number, c.field_name))
