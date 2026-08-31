from __future__ import annotations

import numpy as np
import pandas as pd

from inv_trend.core.features import FeatureCache, FeatureRequest, PreparedBars
from inv_trend.adapters.detector.backtest.engine import ChronologicalSignalValidator
from inv_trend.adapters.detector.models import AssetConfig, Market, StrategyConfig


def _bars(periods: int = 90) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    close = np.linspace(100.0, 160.0, periods)
    return pd.DataFrame(
        {
            "open": np.r_[close[0], close[:-1]], "high": close + 1.0,
            "low": close - 1.0, "close": close, "volume": 1_000.0,
        }, index=index,
    )


def test_feature_cache_reuses_only_identical_input_and_request() -> None:
    bars = _bars()
    cache = FeatureCache()
    request = FeatureRequest.turtle(atr_period=20, channel_periods=(20, 55))
    first = cache.prepare(bars, request)
    assert cache.prepare(bars.copy(), request) is first
    changed = bars.copy()
    changed.iloc[-1, changed.columns.get_loc("close")] += 1.0
    assert cache.prepare(changed, request) is not first


def test_prepared_bars_excludes_current_bar_from_donchian() -> None:
    prepared = PreparedBars.build(
        _bars(), FeatureRequest.turtle(atr_period=5, channel_periods=(5,))
    ).frame
    assert prepared.iloc[20]["channel_high_5"] == prepared.iloc[15:20]["high"].max()


def test_detector_backtest_uses_one_feature_preparation(monkeypatch) -> None:
    calls = 0
    original = FeatureCache.prepare

    def counted(self, bars, request):
        nonlocal calls
        calls += 1
        return original(self, bars, request)

    monkeypatch.setattr(FeatureCache, "prepare", counted)
    asset = AssetConfig(
        symbol="TEST", instrument="TEST", market=Market.CRYPTO, data_source="test",
        timeframes=("D1",), adjustment="none",
    )
    config = StrategyConfig(volatility_lookback=20, long_trend_ma_period=20)
    ChronologicalSignalValidator(config).run(_bars(), asset, "D1")
    assert calls == 1
