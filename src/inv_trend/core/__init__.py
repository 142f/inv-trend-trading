"""Shared, dependency-light primitives for the trend-trading applications."""

from .decision_events import (
    DEFAULT_DECISION_RULE_VERSION,
    ExecutionDecisionEvent,
    ExecutionDecisionEventType,
)
from .events import DecisionEvent, EventType
from .identity import ConfigFingerprint, DataFingerprint, RunId
from .features import FeatureCache, FeatureRequest, PreparedBars, ohlcv_fingerprint
from .math_utils import (
    directional_movement_index,
    exponential_moving_average,
    donchian_channels,
    macd,
    shifted_rolling_high,
    shifted_rolling_low,
    simple_moving_average,
    wilder_atr,
)
from .resampling import aggregate_completed_sessions
from .signals import CORRECTED_STRATEGY_VERSION, SignalEvent, crossed_above, crossed_below
from .performance import equity_statistics

__all__ = [
    "ConfigFingerprint", "DataFingerprint", "DecisionEvent", "EventType", "RunId",
    "DEFAULT_DECISION_RULE_VERSION", "ExecutionDecisionEvent",
    "ExecutionDecisionEventType",
    "FeatureCache", "FeatureRequest", "PreparedBars", "ohlcv_fingerprint", "equity_statistics",
    "CORRECTED_STRATEGY_VERSION",
    "SignalEvent", "crossed_above", "crossed_below", "donchian_channels",
    "directional_movement_index", "aggregate_completed_sessions",
    "exponential_moving_average", "macd", "shifted_rolling_high",
    "shifted_rolling_low", "simple_moving_average", "wilder_atr",
]
