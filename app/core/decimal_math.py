from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
PRICE_QUANTUM = Decimal("0.000000000001")
QUANTITY_QUANTUM = Decimal("0.000000000001")
MONEY_QUANTUM = Decimal("0.0000000001")
RATE_QUANTUM = Decimal("0.000000000001")


def decimal(value: Any) -> Decimal:
    """Convert through text so binary floats never leak their representation."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def price(value: Any) -> Decimal:
    return decimal(value).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def quantity(value: Any, step: Any | None = None) -> Decimal:
    result = decimal(value)
    if step is not None and decimal(step) > ZERO:
        increment = decimal(step)
        result = (result / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    return result.quantize(QUANTITY_QUANTUM, rounding=ROUND_DOWN)


def money(value: Any) -> Decimal:
    return decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def rate(value: Any) -> Decimal:
    return decimal(value).quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
