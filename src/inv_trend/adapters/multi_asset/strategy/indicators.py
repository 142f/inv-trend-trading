"""Turtle indicator calculations."""

from __future__ import annotations

import pandas as pd

from inv_trend.core.features import FeatureRequest, PreparedBars
from inv_trend.core.math_utils import rolling_percentile

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
    request = FeatureRequest.turtle(
        atr_period=rules.n_period,
        channel_periods=periods,
        sma_lags=((rules.entry_ma_period, 0),) if rules.entry_ma_period >= 2 else (),
    )
    if rules.entry_adx_min > 0:
        request = FeatureRequest.merge(request, FeatureRequest(dmi_period=rules.entry_adx_period))
    prepared = PreparedBars.build(bars, request)
    out = prepared.frame.copy()
    out["n"] = out["atr"]
    for period in periods:
        # Compatibility aliases for existing strategy and result baselines.
        out[f"high_{period}"] = out[f"channel_high_{period}"]
        out[f"low_{period}"] = out[f"channel_low_{period}"]
    if rules.entry_ma_period >= 2:
        out[f"sma_{rules.entry_ma_period}"] = out[f"sma_{rules.entry_ma_period}_lag_0"]
    if rules.entry_atr_percentile_max < 1.0:
        # Relative ATR makes the trailing volatility comparison independent
        # of the absolute price scale; use the shared causal mid-rank primitive.
        out["entry_atr_percentile"] = rolling_percentile(
            out["n"] / out["close"], rules.entry_atr_percentile_lookback,
            min_periods=rules.entry_atr_percentile_lookback,
        )
    confirmation = rules.entry_confirmation_bars
    if confirmation > 1:
        low_close = out["close"].rolling(confirmation, min_periods=confirmation).min()
        high_close = out["close"].rolling(confirmation, min_periods=confirmation).max()
        complete = pd.Series(True, index=out.index)
        if "is_complete" in out:
            complete = out["is_complete"].eq(True).rolling(confirmation, min_periods=confirmation).sum().eq(confirmation)
        buffer = rules.breakout_buffer_n * out["n"].shift(confirmation - 1)
        for period in {rules.fast_entry, rules.slow_entry}:
            # Freeze the channel immediately before the first close in this
            # window. Later confirmation bars must not move their own trigger.
            upper = out[f"high_{period}"].shift(confirmation - 1)
            lower = out[f"low_{period}"].shift(confirmation - 1)
            out[f"entry_anchor_high_{period}"] = upper
            out[f"entry_anchor_low_{period}"] = lower
            out[f"entry_confirm_long_{period}"] = (low_close > upper + buffer) & complete
            out[f"entry_confirm_short_{period}"] = (high_close < lower - buffer) & complete

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
    if rules.entry_adx_min > 0:
        columns.add("adx")
    if rules.entry_atr_percentile_max < 1.0:
        columns.add("entry_atr_percentile")
    if rules.entry_confirmation_bars > 1:
        for period in {rules.fast_entry, rules.slow_entry}:
            columns.update(f"entry_{name}_{period}" for name in (
                "anchor_high", "anchor_low", "confirm_long", "confirm_short"))
    return columns


def _indicator_rules_key(rules: TurtleRules) -> tuple:
    return (
        rules.n_period,
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
        rules.entry_ma_period,
        rules.entry_adx_period if rules.entry_adx_min > 0 else 0,
        rules.entry_atr_percentile_lookback if rules.entry_atr_percentile_max < 1.0 else 0,
        rules.entry_confirmation_bars,
        rules.breakout_buffer_n,
    )


def _require_columns(df: pd.DataFrame, columns: set[str]) -> None:
    missing = sorted(columns - set(df.columns))
    if missing:
        raise ValueError(f"missing required bar columns: {missing}")
