from __future__ import annotations

from decimal import Decimal


def parse_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError(f"cannot parse decimal from {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(format(value, "f"))
    if isinstance(value, str):
        return Decimal(value.strip())
    raise ValueError(f"cannot parse decimal from {type(value).__name__}")


def dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
