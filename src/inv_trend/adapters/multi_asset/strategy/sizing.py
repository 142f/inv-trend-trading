"""Position sizing helpers."""

from __future__ import annotations

import numpy as np


def _risk_sized_qty(
    equity: float,
    unit_1n_risk_pct: float,
    n: float,
    point_value: float,
    qty_step: float,
) -> float:
    if equity <= 0 or unit_1n_risk_pct <= 0 or n <= 0 or point_value <= 0:
        return 0.0
    raw_qty = equity * unit_1n_risk_pct / (n * point_value)
    return _round_down(raw_qty, qty_step)


def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return float(np.floor(value / step) * step)
