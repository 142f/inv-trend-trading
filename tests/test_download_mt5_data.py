from __future__ import annotations

import pandas as pd
import pytest

from examples.download_mt5_data import data_quality, normalize_ohlc


def test_normalize_ohlc_filters_bad_rows() -> None:
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2024-01-01 00:00:00Z", "2024-01-01 04:00:00Z", "2024-01-01 04:00:00Z"]
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 99.0, 103.0],
            "low": [99.0, 100.0, 99.0],
            "close": [100.5, 100.0, 102.0],
        }
    ).set_index("time")
    out = normalize_ohlc(df, symbol="XAUUSDc", timeframe="H4")
    assert len(out) == 1
    assert out["symbol"].iloc[0] == "XAUUSDc"
    assert out["timeframe"].iloc[0] == "H4"


def test_data_quality_reports_basic_fields() -> None:
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(["2024-01-01 00:00:00Z", "2024-01-01 04:00:00Z"]),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "spread": [10.0, 12.0],
        }
    )
    report = data_quality(df, symbol="BTCUSDc", timeframe="H4", point=0.01)
    assert report["bar_count"] == 2
    assert report["market_type"] == "24x7"
    assert report["median_spread"] == 11.0
    assert report["expected_gap_minutes"] == 240.0
    assert report["median_spread_bps"] == pytest.approx(10.83284799068142)


def test_data_quality_separates_xau_session_gaps_from_abnormal_missing_bars() -> None:
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(
                [
                    "2024-01-05 16:00:00Z",
                    "2024-01-05 20:00:00Z",
                    "2024-01-07 20:00:00Z",
                    "2024-01-08 00:00:00Z",
                    "2024-01-08 12:00:00Z",
                ]
            ),
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5],
            "spread": [1.0, 1.0, 2.0, 2.0, 3.0],
        }
    )

    report = data_quality(df, symbol="XAUUSDc", timeframe="H4", point=0.01)

    assert report["market_type"] == "session"
    assert report["large_gap_count"] == 2
    assert report["normal_session_gap_count"] == 1
    assert report["abnormal_gap_count"] == 1
    assert report["max_gap_hours"] == 48.0


def test_data_quality_treats_btc_weekend_gaps_as_abnormal() -> None:
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(
                [
                    "2024-01-05 16:00:00Z",
                    "2024-01-05 20:00:00Z",
                    "2024-01-07 20:00:00Z",
                ]
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "spread": [1.0, 1.0, 2.0],
        }
    )

    report = data_quality(df, symbol="BTCUSDc", timeframe="H4", point=0.01)

    assert report["market_type"] == "24x7"
    assert report["large_gap_count"] == 1
    assert report["normal_session_gap_count"] == 0
    assert report["abnormal_gap_count"] == 1
