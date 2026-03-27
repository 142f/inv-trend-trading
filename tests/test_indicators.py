from __future__ import annotations

import pandas as pd

from indicators.ema_atr import add_atr, add_ema


def test_ema_and_atr_columns_exist():
    idx = pd.date_range("2024-01-01", periods=100, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100 + i * 0.1 for i in range(100)],
            "high": [100 + i * 0.1 + 0.5 for i in range(100)],
            "low": [100 + i * 0.1 - 0.5 for i in range(100)],
            "close": [100 + i * 0.1 for i in range(100)],
            "volume": [10] * 100,
        },
        index=idx,
    )
    out = add_atr(add_ema(df), period=14)
    assert "ema144" in out.columns
    assert "ema169" in out.columns
    assert "atr14" in out.columns
    assert out["atr14"].dropna().shape[0] > 0
