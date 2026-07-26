"""Risk-unit recommendation independent of broker lot rules."""

from __future__ import annotations


def suggested_risk_quantity(
    equity: float,
    risk_fraction: float,
    atr: float,
    point_value: float = 1.0,
) -> float:
    if equity <= 0 or risk_fraction <= 0 or atr <= 0 or point_value <= 0:
        return 0.0
    return equity * risk_fraction / (atr * point_value)
