"""Money as Decimal with an explicit ISO 4217 currency (ADR-008).

No floats anywhere. Arithmetic across currencies raises. Amounts entered by people
must not carry more decimal places than the currency allows (e.g. JPY has none).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

# Minor-unit exponents for supported currencies (ISO 4217). Extend deliberately, not dynamically.
CURRENCY_EXPONENTS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CAD": 2,
    "AUD": 2,
    "NZD": 2,
    "CHF": 2,
    "SGD": 2,
    "HKD": 2,
    "AED": 2,
    "SAR": 2,
    "QAR": 2,
    "INR": 2,
    "PKR": 2,
    "ZAR": 2,
    "SEK": 2,
    "NOK": 2,
    "DKK": 2,
    "PLN": 2,
    "MXN": 2,
    "BRL": 2,
    "CNY": 2,
    "MYR": 2,
    "PHP": 2,
    "THB": 2,
    "TRY": 2,
    "EGP": 2,
    "NGN": 2,
    "KES": 2,
    "BDT": 2,
    "LKR": 2,
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    "UGX": 0,
    "KWD": 3,
    "BHD": 3,
    "OMR": 3,
    "JOD": 3,
    "TND": 3,
}

MAX_AMOUNT = Decimal("999999999999999.9999")  # fits NUMERIC(19, 4)


class MoneyError(ValueError):
    pass


def currency_exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError as exc:
        raise MoneyError(f"Unsupported currency '{currency}'") from exc


def validate_amount(amount: Decimal, currency: str, *, allow_zero: bool = False) -> Decimal:
    """Validate a human-entered amount: finite, in range, and not over-precise for the currency."""
    exponent = currency_exponent(currency)
    try:
        if not amount.is_finite():
            raise MoneyError("Amount must be a finite number")
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise MoneyError("Amount is not a valid number") from exc
    if amount < 0 or (amount == 0 and not allow_zero):
        raise MoneyError(
            "Amount must be greater than zero" if not allow_zero else "Amount must not be negative"
        )
    if amount > MAX_AMOUNT:
        raise MoneyError("Amount is too large")
    quantum = Decimal(1).scaleb(-exponent)
    if amount != amount.quantize(quantum):  # e.g. 10.505 USD, or 100.5 JPY
        raise MoneyError(f"{currency} amounts allow at most {exponent} decimal place(s)")
    return amount.quantize(quantum)


def to_minor_units(amount: Decimal, currency: str) -> Decimal:
    """Present a stored amount in the currency's minor units (e.g. 5200.0000 USD -> 5200.00)."""
    return amount.quantize(Decimal(1).scaleb(-currency_exponent(currency)), rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        currency_exponent(self.currency)
        if not isinstance(self.amount, Decimal):
            raise MoneyError("Money requires a Decimal amount")

    def _same(self, other: Money) -> None:
        if other.currency != self.currency:
            raise MoneyError(f"Cannot combine {self.currency} with {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def rounded(self, rounding: str = ROUND_HALF_EVEN) -> Money:
        """Round once, at the end of a calculation, to the currency's minor unit."""
        quantum = Decimal(1).scaleb(-currency_exponent(self.currency))
        return Money(self.amount.quantize(quantum, rounding=rounding), self.currency)

    def __str__(self) -> str:
        return f"{self.rounded().amount} {self.currency}"
