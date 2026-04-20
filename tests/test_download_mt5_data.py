from __future__ import annotations

import pandas as pd

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
    report = data_quality(df, symbol="BTCUSDc", timeframe="H4")
    assert report["bar_count"] == 2
    assert report["median_spread"] == 11.0
