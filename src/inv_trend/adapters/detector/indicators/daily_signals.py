"""Compatibility exports for pure D1 signal calculations.

The implementation belongs to :mod:`inv_trend.core.strategy.daily.signals`.
This historical adapter path remains available to existing detector callers.
"""

from inv_trend.core.strategy.daily.signals import (
    MACD_FAST_PERIOD,
    MACD_SIGNAL_PERIOD,
    MACD_SLOW_PERIOD,
    SMA_FAST_PERIOD,
    SMA_SLOW_PERIOD,
    TURTLE_PERIODS,
    analyze_daily_signals,
    analyze_prepared_daily_signals,
    daily_signal_feature_request,
    normalize_completed_daily_bars,
    prepare_daily_signal_frame,
)

__all__ = [
    "MACD_FAST_PERIOD",
    "MACD_SIGNAL_PERIOD",
    "MACD_SLOW_PERIOD",
    "SMA_FAST_PERIOD",
    "SMA_SLOW_PERIOD",
    "TURTLE_PERIODS",
    "analyze_daily_signals",
    "analyze_prepared_daily_signals",
    "daily_signal_feature_request",
    "normalize_completed_daily_bars",
    "prepare_daily_signal_frame",
]
