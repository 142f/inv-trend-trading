"""Shared, dependency-light primitives for the trend-trading applications."""

from .events import DecisionEvent, EventType
from .identity import ConfigFingerprint, DataFingerprint, RunId
from .features import FeatureCache, FeatureRequest, PreparedBars, ohlcv_fingerprint
from .math_utils import (
    exponential_moving_average,
    donchian_channels,
    macd,
    shifted_rolling_high,
    shifted_rolling_low,
    simple_moving_average,
    wilder_atr,
)
from .signals import CORRECTED_STRATEGY_VERSION, SignalEvent, crossed_above, crossed_below
from .performance import equity_statistics

__all__ = [
    "ConfigFingerprint", "DataFingerprint", "DecisionEvent", "EventType", "RunId",
    "FeatureCache", "FeatureRequest", "PreparedBars", "ohlcv_fingerprint", "equity_statistics",
    "CORRECTED_STRATEGY_VERSION",
    "SignalEvent", "crossed_above", "crossed_below", "donchian_channels",
    "exponential_moving_average", "macd", "shifted_rolling_high",
    "shifted_rolling_low", "simple_moving_average", "wilder_atr",
]
