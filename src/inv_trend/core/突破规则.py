"""Causal breakout direction shared by live order generation and replay evidence."""
from __future__ import annotations

import math
from typing import Any, Mapping


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def breakout_direction(
    row: Mapping[str, Any], period: int, n: float, *, buffer_n: float = 0., trigger_mode: str = "close"
) -> int | None:
    upper, lower = _number(row.get(f"high_{period}")), _number(row.get(f"low_{period}"))
    if upper is None or lower is None:
        return None
    upper, lower = upper + buffer_n * n, lower - buffer_n * n
    if trigger_mode == "intraday":
        high, low = _number(row.get("high")), _number(row.get("low"))
        long_hit, short_hit = high is not None and high > upper, low is not None and low < lower
        return None if long_hit == short_hit else 1 if long_hit else -1
    close = _number(row.get("close"))
    if close is not None and close > upper:
        return 1
    if close is not None and close < lower:
        return -1
    return None
