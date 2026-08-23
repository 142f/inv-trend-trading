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
    seed = float(values.iloc[:period].mean())
    # Pandas' recursive EWM is the same Wilder recurrence once its first
    # observation is the canonical arithmetic seed.  Market-quality frames
    # have no gaps, so this runs in vectorized C code.  Preserve the former
    # conservative NaN propagation for malformed/gappy series below.
    tail = values.iloc[period:]
    if tail.notna().all():
        seeded = pd.concat(
            (pd.Series([seed], dtype=float), tail.reset_index(drop=True)),
            ignore_index=True,
        )
        out.iloc[period - 1 :] = seeded.ewm(
            alpha=1.0 / period, adjust=False
        ).mean().to_numpy()
        return out
    out.iloc[period - 1] = seed
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


def simple_moving_average(values: pd.Series, period: int, *, lag: int = 0) -> pd.Series:
    """Causal simple moving average shared by research and live scanners."""
    _numeric_series(values, "values")
    if period < 2:
        raise ValueError("period must be >= 2")
    if lag < 0:
        raise ValueError("lag must be non-negative")
    return values.rolling(period, min_periods=period).mean().shift(lag)


def exponential_moving_average(values: pd.Series, period: int) -> pd.Series:
    """Conventional recursive EMA (``adjust=False``)."""
    _numeric_series(values, "values")
    if period < 2:
        raise ValueError("period must be >= 2")
    return values.ewm(span=period, adjust=False).mean()


def macd(
    values: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Return EMA, DIF, DEA and histogram series using one shared definition."""
    if not 1 < fast < slow or signal < 2:
        raise ValueError("MACD periods require 1 < fast < slow and signal >= 2")
    ema_fast = exponential_moving_average(values, fast)
    ema_slow = exponential_moving_average(values, slow)
    dif = ema_fast - ema_slow
    dea = exponential_moving_average(dif, signal)
    return pd.DataFrame(
        {
            f"ema_{fast}": ema_fast,
            f"ema_{slow}": ema_slow,
            "dif": dif,
            "dea": dea,
            "histogram": dif - dea,
            "macd_bar": 2.0 * (dif - dea),
        }
    )


def directional_movement_index(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    """Return the standard Wilder ``+DI``, ``-DI``, DX and ADX series.

    The calculation intentionally shares the same Wilder seeding convention as
    :func:`wilder_atr`: the first available smoothed value is the arithmetic
    mean of the first ``period`` observations, and later values use the Wilder
    recurrence.  This makes the feature safe for both reports and replayed
    historical scans.
    """

    for name, value in (("high", high), ("low", low), ("close", close)):
        _numeric_series(value, name)
    if not high.index.equals(low.index) or not high.index.equals(close.index):
        raise ValueError("high, low, close indexes must match")
    if period < 2:
        raise ValueError("period must be >= 2")

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0),
        index=high.index,
        dtype=float,
        name="plus_dm",
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=high.index,
        dtype=float,
        name="minus_dm",
    )
    # The first directional move has no previous bar, so it contributes zero
    # to the first Wilder seed (matching the true-range treatment of bar 0).
    plus_dm.iloc[0] = 0.0
    minus_dm.iloc[0] = 0.0

    atr = wilder_atr(high, low, close, period)
    plus_smoothed = wilder_average(plus_dm, period)
    minus_smoothed = wilder_average(minus_dm, period)
    plus_di = 100.0 * plus_smoothed / atr
    minus_di = 100.0 * minus_smoothed / atr
    denominator = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / denominator).where(
        denominator > 0.0
    )
    adx = pd.Series(np.nan, index=dx.index, dtype=float, name="adx")
    first_dx = dx.first_valid_index()
    if first_dx is not None:
        # DX starts only after the first DI seed.  Smooth its contiguous tail
        # rather than letting leading NaNs suppress the whole ADX series.
        smoothed = wilder_average(dx.loc[first_dx:], period)
        adx.loc[smoothed.index] = smoothed
    return pd.DataFrame(
        {
            "plus_dm": plus_dm,
            "minus_dm": minus_dm,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "dx": dx,
            "adx": adx,
        }
    )


def donchian_channels(
    high: pd.Series, low: pd.Series, periods: set[int] | tuple[int, ...]
) -> pd.DataFrame:
    """Canonical prior-bar Donchian features with stable column names."""
    if not high.index.equals(low.index):
        raise ValueError("high and low indexes must match")
    out = pd.DataFrame(index=high.index)
    for period in sorted(set(periods)):
        out[f"channel_high_{period}"] = shifted_rolling_high(high, period)
        out[f"channel_low_{period}"] = shifted_rolling_low(low, period)
    return out


def wilder_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20
) -> pd.Series:
    result = wilder_average(true_range(high, low, close), period)
    result.name = "atr"
    return result


def rolling_percentile(values: pd.Series, lookback: int) -> pd.Series:
    """Causal percentile rank of each value within its trailing window."""
    _numeric_series(values, "values")
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    minimum = min(lookback, max(2, lookback // 4))
    return values.rolling(lookback, min_periods=minimum).apply(
        lambda window: float((window <= window.iloc[-1]).mean()), raw=False
    )
