"""Volatility-scaled stop and pyramid levels."""

from __future__ import annotations

from ..models import Direction


def initial_stop(price: float, atr: float, direction: Direction, multiple: float) -> float:
    if direction is Direction.LONG:
        return price - multiple * atr
    if direction is Direction.SHORT:
        return price + multiple * atr
    raise ValueError("stop requires a long or short direction")


def next_add_price(price: float, atr: float, direction: Direction, step: float) -> float:
    if direction is Direction.LONG:
        return price + step * atr
    if direction is Direction.SHORT:
        return price - step * atr
    raise ValueError("pyramid level requires a long or short direction")
