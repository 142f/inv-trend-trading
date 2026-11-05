"""Exact lot/step rounding for every order-sizing boundary.

Binary floats cannot represent most decimal steps exactly, so the naive
``floor(quantity / step) * step`` drops one whole lot whenever the quotient
lands a few ULPs below an integer:

    0.3 / 0.1 == 2.9999999999999996  ->  floor -> 2  ->  0.2   (one lot lost)

The loss is silent and always in the *conservative* direction for the account,
which is why it survived: cash never goes negative and no invariant fires, but
the position is smaller than the risk budget allows, so every downstream
exposure/return statistic is measured on a different position than the one the
strategy asked for.  Order sizing must therefore never divide by a step on its
own; it goes through this module.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
import math

__all__ = ["floor_to_step", "step_decimals"]

# A quotient within this relative tolerance of an integer is treated as that
# integer.  It is far below any economically meaningful fraction of a lot
# (1e-9 lots) and far above the accumulated representation error of a double
# division (~1e-16 relative).
_QUOTIENT_TOLERANCE = 1e-9

# Snap results to the step's own decimal resolution so callers can compare and
# serialise quantities (0.3, not 0.30000000000000004).
_MAX_DECIMALS = 12


@lru_cache(maxsize=256)
def step_decimals(step: float) -> int:
    """Return the decimal places needed to represent ``step`` exactly."""

    try:
        exponent = Decimal(str(float(step))).as_tuple().exponent
    except (InvalidOperation, ValueError):
        return _MAX_DECIMALS
    decimals = -int(exponent) if exponent < 0 else 0
    return min(max(decimals, 0), _MAX_DECIMALS)


def floor_to_step(value: float, step: float, *, name: str = "quantity") -> float:
    """Round ``value`` down to an exact multiple of ``step``.

    Raises ``ValueError`` for a non-finite value or a non-positive step instead
    of returning a silently wrong size.
    """

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name}: numeric value required")
    if not math.isfinite(value) or not math.isfinite(step):
        raise ValueError(f"{name}: finite value and step required")
    if step <= 0:
        raise ValueError(f"{name}: positive step required")
    quotient = value / step
    nearest = round(quotient)
    tolerance = _QUOTIENT_TOLERANCE * max(1.0, abs(nearest))
    steps = nearest if abs(quotient - nearest) <= tolerance else math.floor(quotient)
    return round(steps * step, step_decimals(step))
