"""Permanent regressions migrated from the historical refactor evidence script."""

from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
import pandas as pd

from inv_trend.core import features, math_utils


def test_shared_features_compute_each_average_once_and_reuse_cached_result():
    close = np.linspace(100, 120, 200)
    frame = pd.DataFrame(
        dict(open=close, high=close + 1, low=close - 1, close=close, volume=1000.0),
        index=pd.date_range("2024-01-01", periods=len(close), tz="UTC"),
    )
    request = features.FeatureRequest(
        sma_lags=tuple((period, lag) for period in (10, 20, 60) for lag in (0, 1, 2)),
        ema_periods=(12, 26, 60),
        macd_periods=(12, 26, 9),
        volume_sma_lags=((20, 0), (20, 1)),
    )
    with ExitStack() as stack:
        probes = [
            stack.enter_context(patch.object(owner, name, wraps=getattr(owner, name)))
            for owner, name in (
                (features, "_validate_ohlcv"),
                (features, "simple_moving_average"),
                (features, "exponential_moving_average"),
                (math_utils, "exponential_moving_average"),
            )
        ]
        cache = features.FeatureCache()
        first = cache.prepare(frame, request)
        assert [probe.call_count for probe in probes] == [1, 4, 3, 1]
        assert cache.prepare(frame.copy(), request) is first
        assert [probe.call_count for probe in probes] == [2, 4, 3, 1]


def test_breakout_direction_matches_close_rule_at_boundaries_and_with_buffer():
    from inv_trend.core.突破规则 import breakout_direction

    for buffer in (0.0, 0.25, 1.0):
        upper, lower = 105.0 + buffer * 2.0, 95.0 - buffer * 2.0
        for close in np.r_[np.linspace(90, 110, 201), upper, lower]:
            expected = 1 if close > upper else -1 if close < lower else None
            row = dict(high_20=105.0, low_20=95.0, close=close)
            assert breakout_direction(row, 20, 2.0, buffer_n=buffer) == expected
