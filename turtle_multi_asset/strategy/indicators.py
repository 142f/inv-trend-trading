"""Turtle indicator calculations."""

from __future__ import annotations

import pandas as pd

from inv_trend_core.features import FeatureRequest, PreparedBars

from ..models.domain import TurtleRules


def compute_turtle_indicators(bars: pd.DataFrame, rules: TurtleRules) -> pd.DataFrame:
    """Return bars with Wilder N and shifted breakout/exit channels."""

    _require_columns(bars, {"open", "high", "low", "close"})
    periods = {
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    }
    prepared = PreparedBars.build(
        bars,
        FeatureRequest.turtle(
            atr_period=rules.n_period,
            channel_periods=periods,
            sma_lags=((rules.entry_ma_period, 0),) if rules.entry_ma_period >= 2 else (),
        ),
    )
    out = prepared.frame.copy()
    out["n"] = out["atr"]
    for period in periods:
        # Compatibility aliases for existing strategy and result baselines.
        out[f"high_{period}"] = out[f"channel_high_{period}"]
        out[f"low_{period}"] = out[f"channel_low_{period}"]
    if rules.entry_ma_period >= 2:
        out[f"sma_{rules.entry_ma_period}"] = out[f"sma_{rules.entry_ma_period}_lag_0"]

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
    if rules.entry_ma_period >= 2:
        columns.add(f"sma_{rules.entry_ma_period}")
    return columns


def _indicator_rules_key(rules: TurtleRules) -> tuple[int, int, int, int, int, int]:
    return (
        rules.n_period,
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
        rules.entry_ma_period,
    )


def _require_columns(df: pd.DataFrame, columns: set[str]) -> None:
    missing = sorted(columns - set(df.columns))
    if missing:
        raise ValueError(f"missing required bar columns: {missing}")
