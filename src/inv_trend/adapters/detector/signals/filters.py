"""Quality filters applied after the raw Turtle event is identified."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..models import AssetConfig, Direction, StrategyConfig


def evaluate_filters(
    row: Mapping[str, Any],
    direction: Direction,
    asset: AssetConfig,
    config: StrategyConfig,
) -> tuple[str, ...]:
    reasons: list[str] = []
    atr = float(row["atr"])
    close = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])

    if float(row.get("volume", 0.0)) < asset.min_volume:
        reasons.append("below_minimum_liquidity")
    volume_mean = float(row.get("volume_mean", np.nan))
    if (
        config.volume_filter
        and np.isfinite(volume_mean)
        and float(row["volume"]) < volume_mean * config.min_volume_ratio
    ):
        reasons.append("volume_below_filter")

    bar_range = high - low
    if config.close_location_filter and bar_range > 0:
        close_location = (close - low) / bar_range
        if direction is Direction.LONG and close_location < config.min_close_location:
            reasons.append("weak_close_location")
        if direction is Direction.SHORT and close_location > 1 - config.min_close_location:
            reasons.append("weak_close_location")

    if config.trend_filter:
        ma = float(row.get(f"sma_{config.trend_ma_period}", np.nan))
        long_ma = float(row.get(f"sma_{config.long_trend_ma_period}", np.nan))
        if not np.isfinite(ma) or not np.isfinite(long_ma):
            reasons.append("trend_filter_not_warm")
        elif direction is Direction.LONG and (close < ma or close < long_ma):
            reasons.append("below_trend_average")
        elif direction is Direction.SHORT and (close > ma or close > long_ma):
            reasons.append("above_trend_average")

    previous_close = float(row.get("previous_close", np.nan))
    if np.isfinite(previous_close) and atr > 0:
        gap_atr = abs(open_price - previous_close) / atr
        if gap_atr > config.max_gap_atr:
            reasons.append("opening_gap_too_large")
    return tuple(reasons)
