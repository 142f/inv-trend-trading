from __future__ import annotations

import pandas as pd

from data.aggregator import aggregate_from_daily


def test_aggregate_2d_complete_windows():
    idx = pd.date_range("2024-01-01", periods=10, freq="1d", tz="UTC")
    df = pd.DataFrame(
        {
            "open": range(10),
            "high": [x + 1 for x in range(10)],
            "low": [x - 1 for x in range(10)],
            "close": [x + 0.5 for x in range(10)],
            "volume": [100] * 10,
            "close_ts": idx + pd.Timedelta(days=1),
        },
        index=idx,
    )
    out = aggregate_from_daily(df, 2)
    # Epoch-anchored 2D buckets can drop head/tail partial windows.
    assert len(out) == 4
    assert float(out.iloc[0]["open"]) == 1
    assert float(out.iloc[0]["close"]) == 2.5
    assert float(out.iloc[0]["volume"]) == 200
