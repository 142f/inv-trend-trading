from __future__ import annotations

import pandas as pd

from turtle_multi_asset.core_dataset import build_daily_equity_curve, compare_summaries
from turtle_multi_asset.data_pipeline import resample_ohlcv


def test_resample_h4_to_d1_aggregates_ohlcv_without_forward_fill() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-01-01 00:00:00+00:00", "2024-01-01 04:00:00+00:00", "2024-01-01 08:00:00+00:00"]
            ),
            "symbol": ["XAU", "XAU", "XAU"],
            "open": [10.0, 11.0, 12.0],
            "high": [11.0, 12.0, 13.0],
            "low": [9.0, 10.0, 11.0],
            "close": [10.5, 11.5, 12.5],
            "volume": [1.0, 2.0, 3.0],
            "spread": [0.2, 0.4, 0.6],
            "source": ["external", "external", "external"],
            "timeframe": ["H4", "H4", "H4"],
        }
    )

    daily = resample_ohlcv(frame, "D1")

    assert len(daily) == 1
    row = daily.iloc[0]
    assert row["open"] == 10.0
    assert row["high"] == 13.0
    assert row["low"] == 9.0
    assert row["close"] == 12.5
    assert row["volume"] == 6.0


def test_daily_equity_curve_uses_end_of_day_values() -> None:
    equity = pd.Series(
        [100.0, 101.0, 102.0, 105.0],
        index=pd.to_datetime(
            ["2024-01-01 00:00:00+00:00", "2024-01-01 04:00:00+00:00", "2024-01-02 00:00:00+00:00", "2024-01-02 08:00:00+00:00"]
        ),
    )

    daily = build_daily_equity_curve(equity)

    assert daily["equity"].tolist() == [101.0, 105.0]


def test_compare_summaries_flags_identical_results_as_unchanged() -> None:
    baseline = {"total_return": 0.1, "trade_count": 3, "final_equity": 110.0}
    rebuilt = {"total_return": 0.1, "trade_count": 3, "final_equity": 110.0}

    comparison = compare_summaries(baseline, rebuilt)

    assert comparison["changed"] is False
