"""Causal Series-only indicator primitives with explicit input protection."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _numeric_series(value: pd.Series, name: str) -> None:
    if not isinstance(value, pd.Series):
        raise TypeError(f"{name} must be a pandas Series")
    if not pd.api.types.is_numeric_dtype(value):
        raise ValueError(f"{name} must have a numeric dtype")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    for name, value in (("high", high), ("low", low), ("close", close)):
        _numeric_series(value, name)
    if not high.index.equals(low.index) or not high.index.equals(close.index):
        raise ValueError("high, low, close indexes must match")
    previous = close.shift(1)
    return pd.concat((high - low, (high - previous).abs(), (low - previous).abs()), axis=1).max(axis=1)


def wilder_average(values: pd.Series, period: int) -> pd.Series:
    _numeric_series(values, "values")
    if period < 2:
        raise ValueError("period must be >= 2")
    out = pd.Series(np.nan, index=values.index, dtype=float, name=values.name)
    if len(values) < period or values.iloc[:period].isna().any():
        return out
    out.iloc[period - 1] = values.iloc[:period].mean()
    for index in range(period, len(values)):
        if pd.notna(values.iloc[index]) and pd.notna(out.iloc[index - 1]):
            out.iloc[index] = (out.iloc[index - 1] * (period - 1) + values.iloc[index]) / period
    return out


def shifted_rolling_high(values: pd.Series, period: int) -> pd.Series:
    _numeric_series(values, "values")
    if period < 2:
        raise ValueError("period must be >= 2")
    return values.rolling(period).max().shift(1)


def shifted_rolling_low(values: pd.Series, period: int) -> pd.Series:
    _numeric_series(values, "values")
    if period < 2:
        raise ValueError("period must be >= 2")
    return values.rolling(period).min().shift(1)
