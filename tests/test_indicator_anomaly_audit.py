from __future__ import annotations

import numpy as np
import pandas as pd

from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.math_utils import rolling_percentile
from inv_trend.core.strategy.daily.analysis import detect_anomalies, prepare_daily_analysis


def _flat_bars(periods: int = 40) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000.0,
        },
        index=index,
    )


def test_trailing_percentile_uses_midrank_for_tied_values() -> None:
    values = pd.Series([1.0] * 8, dtype=float)

    result = rolling_percentile(values, 5, min_periods=5)

    assert result.iloc[:4].isna().all()
    assert result.iloc[4:].tolist() == [0.5] * 4


def test_trailing_percentile_has_symmetric_finite_extremes() -> None:
    increasing = rolling_percentile(
        pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 5, min_periods=5
    )
    decreasing = rolling_percentile(
        pd.Series([5.0, 4.0, 3.0, 2.0, 1.0]), 5, min_periods=5
    )

    assert increasing.iloc[-1] == 0.9
    assert decreasing.iloc[-1] == 0.1


def test_price_anomalies_use_previous_completed_bar_atr() -> None:
    bars = _flat_bars()
    bars.iloc[-1, bars.columns.get_loc("open")] = 160.0
    bars.iloc[-1, bars.columns.get_loc("high")] = 160.0
    bars.iloc[-1, bars.columns.get_loc("low")] = 40.0
    prepared = prepare_daily_analysis(
        bars,
        DailyChecksConfig(atr_percentile_lookback=20),
        session_anchor=bars.index[0],
    )

    anomalies = detect_anomalies(prepared, start_position=len(bars) - 1)
    by_type = {item.anomaly_type: item for item in anomalies}
    row = prepared.base.iloc[-1]

    assert row["previous_atr"] == 2.0
    assert row["gap_atr_ratio"] == 30.0
    assert row["range_atr_ratio"] == 60.0
    assert by_type["gap_extreme"].conditions[0].actual_value == 30.0
    assert by_type["gap_extreme"].conditions[0].reference_value == 2.0
    assert by_type["range_extreme"].conditions[0].actual_value == 60.0
    assert by_type["range_extreme"].conditions[0].reference_value == 3.0
    assert by_type["range_extreme"].conditions[0].metadata["atr_reference"] == 2.0
    assert row["atr"] > row["previous_atr"]


def test_anomaly_threshold_equality_does_not_trigger() -> None:
    bars = _flat_bars()
    bars.iloc[-1, bars.columns.get_loc("open")] = 104.0
    bars.iloc[-1, bars.columns.get_loc("high")] = 104.0
    bars.iloc[-1, bars.columns.get_loc("low")] = 98.0
    prepared = prepare_daily_analysis(
        bars,
        DailyChecksConfig(atr_percentile_lookback=20),
        session_anchor=bars.index[0],
    )

    anomaly_types = {
        item.anomaly_type
        for item in detect_anomalies(prepared, start_position=len(bars) - 1)
    }

    assert prepared.base.iloc[-1]["gap_atr_ratio"] == 2.0
    assert prepared.base.iloc[-1]["range_atr_ratio"] == 3.0
    assert "gap_extreme" not in anomaly_types
    assert "range_extreme" not in anomaly_types


def test_prepared_indicator_history_is_not_changed_by_future_bars() -> None:
    bars = _flat_bars(45)
    future = _flat_bars(5)
    future.index = pd.date_range(bars.index[-1] + pd.Timedelta(days=1), periods=5, freq="D")
    config = DailyChecksConfig(atr_percentile_lookback=20)
    first = prepare_daily_analysis(bars, config, session_anchor=bars.index[0]).base
    extended = prepare_daily_analysis(
        pd.concat((bars, future)), config, session_anchor=bars.index[0]
    ).base.loc[bars.index]

    columns = [
        "channel_high_20",
        "sma_20",
        "ema_144",
        "dif",
        "dea",
        "atr",
        "atr_percentile",
        "relative_volume",
        "gap_atr_ratio",
        "range_atr_ratio",
    ]
    for column in columns:
        assert np.allclose(first[column], extended[column], equal_nan=True)
