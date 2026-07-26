"""Classic Wilder N/ATR."""

from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(bars: pd.DataFrame) -> pd.Series:
    previous_close = bars["close"].shift(1)
    return pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - previous_close).abs(),
            (bars["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def wilder_atr(bars: pd.DataFrame, period: int = 20) -> pd.Series:
    if period < 2:
        raise ValueError("ATR period must be >= 2")
    values = true_range(bars).to_numpy(dtype=float)
    output = np.full(len(values), np.nan)
    if len(values) < period:
        return pd.Series(output, index=bars.index, name="atr")
    seed = values[:period]
    if not np.isfinite(seed).all():
        return pd.Series(output, index=bars.index, name="atr")
    output[period - 1] = seed.mean()
    for idx in range(period, len(values)):
        output[idx] = (output[idx - 1] * (period - 1) + values[idx]) / period
    return pd.Series(output, index=bars.index, name="atr")


def rolling_percentile(values: pd.Series, lookback: int) -> pd.Series:
    def percentile(window: pd.Series) -> float:
        return float((window <= window.iloc[-1]).mean())

    minimum = min(lookback, max(2, lookback // 4))
    return values.rolling(lookback, min_periods=minimum).apply(
        percentile,
        raw=False,
    )
