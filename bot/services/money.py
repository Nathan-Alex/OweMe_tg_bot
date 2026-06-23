from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ..currency import is_currency_token, normalize_currency_code

TWO_DP = Decimal("0.01")
ZERO = Decimal("0.00")


def parse_amount_and_currency(raw_text: str, default_currency: str) -> tuple[Decimal, str]:
    tokens = raw_text.strip().split()
    if not tokens:
        raise ValueError("Amount is required.")

    if len(tokens) > 2:
        raise ValueError("Use only amount and optional currency.")

    amount = parse_amount(tokens[0])
    currency = normalize_currency_code(default_currency)

    if len(tokens) == 2:
        if not is_currency_token(tokens[1]):
            raise ValueError("Currency must be 3 letters, like USD.")
        currency = normalize_currency_code(tokens[1])

    return amount, currency


def parse_amount(raw_value: str) -> Decimal:
    cleaned = raw_value.strip().replace(" ", "")
    if not cleaned:
        raise ValueError("Amount is required.")

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    elif "," in cleaned:
        if cleaned.count(",") == 1 and len(cleaned.split(",", maxsplit=1)[1]) <= 2:
            normalized = cleaned.replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    else:
        normalized = cleaned

    try:
        amount = Decimal(normalized).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid amount.") from exc

    if amount <= ZERO:
        raise ValueError("Amount must be greater than zero.")
    return amount


def to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value)).quantize(TWO_DP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ZERO


def format_amount_compact(amount: Decimal) -> str:
    normalized = amount.quantize(TWO_DP, rounding=ROUND_HALF_UP)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text

