"""Rule-based line-item parsing from estimate / invoice / bill page text (grounded by construction).

A line item is a text line that ends in a money amount and is not a total, tax or balance line.
Amounts come from the text with the shared money normaliser; the model is never asked for
amounts (it may only re-categorise an item, see the assessment agent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from document_processing.extract import normalise_money

_TAIL = re.compile(
    r"^(?P<desc>.*?[A-Za-z].*?)[\s:\-\u2013]+"
    r"(?P<amount>(?:USD|GBP|EUR|PKR|AED|INR|CAD|AUD|US\$|\$|£|€|Rs\.?)?\s?\d[\d,]*\.\d{2})"
    r"\s*(?:USD|GBP|EUR|PKR|AED|INR|CAD|AUD)?\s*$"
)
_SKIP = re.compile(
    r"\b(sub\s*-?total|total|vat|tax|gst|balance|amount (?:due|payable|paid)|deposit|excess|"
    r"discount|payment|deductible)\b",
    re.IGNORECASE,
)
_HEADER = re.compile(r"\b(no|number|date|ref|reference)\b\s*[:#]", re.IGNORECASE)
_QTY = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:x|\N{MULTIPLICATION SIGN}|hours?|hrs?|nights?|days?|units?)\b", re.IGNORECASE
)

MOTOR_CATEGORIES = {
    "labour": r"\b(labou?r|fitting|hours?|hrs)\b",
    "paint": r"\b(paint|refinish|panel|respray|blend)\b",
    "parts": r"\b(parts?|bumper|headlamp|headlight|mirror|bonnet|hood|door|wing|fender|windscreen|"
    r"windshield|glass|grille|radiator|tyre|tire|wheel|sensor|lamp|replacement)\b",
}
HEALTH_CATEGORIES = {
    "room": r"\b(ward|room|bed|nights?|accommodation|icu)\b",
    "procedure": r"\b(procedure|surgery|operation|theatre|appendectomy|\w+ectomy|\w+otomy|\w+plasty)\b",
    "medication": r"\b(pharmacy|medication|medicine|drugs?|prescription)\b",
    "diagnostics": r"\b(lab|laboratory|x-?ray|mri|ct scan|scan|ultrasound|pathology|radiology|tests?)\b",
    "consultation": r"\b(consult\w*|specialist|physician|doctor|visit)\b",
}
CATEGORIES = {"motor": (*MOTOR_CATEGORIES, "other"), "health": (*HEALTH_CATEGORIES, "other")}


@dataclass(frozen=True, slots=True)
class ParsedLine:
    page_number: int
    line_no: int
    description: str
    amount: Decimal
    currency: str | None
    quantity: Decimal | None
    category: str
    excerpt: str


def categorise(description: str, line: str) -> str:
    table = HEALTH_CATEGORIES if line == "health" else MOTOR_CATEGORIES
    for category, pattern in table.items():
        if re.search(pattern, description, re.IGNORECASE):
            return category
    return "other"


def parse_line_items(pages: list[tuple[int, str]], line: str) -> list[ParsedLine]:
    items: list[ParsedLine] = []
    for page_number, text in pages:
        for raw in text.splitlines():
            stripped = raw.strip()
            m = _TAIL.match(stripped)
            if not m:
                continue
            desc = m["desc"].strip(" -:\N{EN DASH}")
            if _SKIP.search(desc) or _HEADER.search(stripped):
                continue
            money = normalise_money(m["amount"].strip())
            if not money or money["amount"] is None:
                continue
            qty = _QTY.search(desc)
            items.append(
                ParsedLine(
                    page_number=page_number,
                    line_no=len(items) + 1,
                    description=desc,
                    amount=Decimal(money["amount"]),
                    currency=money["currency"],
                    quantity=Decimal(qty[1]) if qty else None,
                    category=categorise(desc, line),
                    excerpt=stripped[:500],
                )
            )
    return items
