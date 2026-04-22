"""Turtle indicator calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .domain import TurtleRules


def compute_turtle_indicators(bars: pd.DataFrame, rules: TurtleRules) -> pd.DataFrame:
    """Return bars with Wilder N and shifted breakout/exit channels."""

    _require_columns(bars, {"open", "high", "low", "close"})
    out = bars.copy()
    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["tr"] = true_range
    out["n"] = _wilder_average(true_range, rules.n_period)

    periods = {
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    }
    for period in periods:
        out[f"high_{period}"] = out["high"].rolling(period).max().shift(1)
        out[f"low_{period}"] = out["low"].rolling(period).min().shift(1)

    out.attrs["_turtle_rules_key"] = _indicator_rules_key(rules)
    return out


def _with_indicators(bars: pd.DataFrame, rules: TurtleRules) -> pd.DataFrame:
    required = _indicator_columns(rules)
    if (
        required.issubset(bars.columns)
        and bars.attrs.get("_turtle_rules_key") == _indicator_rules_key(rules)
    ):
        return bars
    return compute_turtle_indicators(bars, rules)


def _indicator_columns(rules: TurtleRules) -> set[str]:
    periods = {
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    }
    columns = {"tr", "n"}
    for period in periods:
        columns.add(f"high_{period}")
        columns.add(f"low_{period}")
    return columns


def _indicator_rules_key(rules: TurtleRules) -> tuple[int, int, int, int, int]:
    return (
        rules.n_period,
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    )


def _wilder_average(values: pd.Series, period: int) -> pd.Series:
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    if len(arr) < period:
        return pd.Series(out, index=values.index)

    seed = arr[:period]
    if not np.all(np.isfinite(seed)):
        return pd.Series(out, index=values.index)
    out[period - 1] = float(np.mean(seed))
    for idx in range(period, len(arr)):
        if np.isfinite(arr[idx]) and np.isfinite(out[idx - 1]):
            out[idx] = (out[idx - 1] * (period - 1) + arr[idx]) / period
    return pd.Series(out, index=values.index)


def _require_columns(df: pd.DataFrame, columns: set[str]) -> None:
    missing = sorted(columns - set(df.columns))
    if missing:
        raise ValueError(f"missing required bar columns: {missing}")
