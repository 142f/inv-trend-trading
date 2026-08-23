from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.daily_analysis import prepare_daily_analysis
from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.features import FeatureRequest, PreparedBars


def _bars(n: int = 260) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    x = np.arange(n, dtype=float)
    close = 100 + 0.18 * x + 4 * np.sin(x / 9)
    op = close + 0.4 * np.sin(x / 5)
    return pd.DataFrame(
        {
            "open": op,
            "high": np.maximum(op, close) + 1.5,
            "low": np.minimum(op, close) - 1.4,
            "close": close,
            "volume": 1000 + 20 * np.cos(x / 7),
            "is_complete": True,
            "dataset_version": "test-v2",
        },
        index=idx,
    )


def test_feature_request_merge_is_minimal_superset() -> None:
    a = FeatureRequest(atr_period=14, donchian_periods=(20,), sma_lags=((10, 0),))
    b = FeatureRequest(
        atr_period=14,
        donchian_periods=(20, 55),
        sma_lags=((20, 0),),
        ema_periods=(144, 169),
        macd_periods=(12, 26, 9),
        dmi_period=14,
    )
    merged = FeatureRequest.merge(a, b)
    assert merged.atr_period == 14
    assert merged.donchian_periods == (20, 55)
    assert merged.sma_lags == ((10, 0), (20, 0))
    assert merged.ema_periods == (144, 169)
    assert merged.macd_periods == (12, 26, 9)
    assert merged.dmi_period == 14


def test_feature_request_merge_rejects_semantic_conflict() -> None:
    with pytest.raises(ValueError, match="conflicting atr_period"):
        FeatureRequest.merge(FeatureRequest(atr_period=14), FeatureRequest(atr_period=20))


def test_daily_analysis_builds_d1_feature_frame_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = PreparedBars.build.__func__

    def counted(cls, bars, request):
        calls.append(len(bars))
        return original(cls, bars, request)

    monkeypatch.setattr(PreparedBars, "build", classmethod(counted))
    bars = _bars()
    # D1 only prevents higher-session MACD aggregation from obscuring this invariant.
    cfg = DailyChecksConfig(macd_session_periods=(1,))
    prepared = prepare_daily_analysis(bars, cfg, session_anchor=bars.index[0])
    assert calls == [len(bars)]
    assert prepared.feature_request.macd_periods == (12, 26, 9)
    assert {"channel_high_20", "channel_high_55", "sma_120", "adx", "atr_percentile"}.issubset(
        prepared.base.columns
    )
