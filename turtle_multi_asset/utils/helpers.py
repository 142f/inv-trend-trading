"""Numeric helpers shared across strategy and backtest modules."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..models.domain import AssetSpec


def finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def round_down(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return float(np.floor(value / step) * step)


def trade_cost(qty: float, price: float, spec: AssetSpec) -> float:
    notional = abs(qty * price * spec.point_value)
    return notional * (spec.cost_bps + spec.slippage_bps) / 10000.0
