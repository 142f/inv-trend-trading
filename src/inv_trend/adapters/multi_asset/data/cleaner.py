"""Deterministic OHLCV cleaning rules."""

from __future__ import annotations

import pandas as pd


def clean_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Clean rows without filling prices or altering valid OHLC values."""

    out = df.copy()
    out = out.drop_duplicates(subset=["date", "symbol"], keep="last")
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out[
        (out["open"] > 0)
        & (out["high"] > 0)
        & (out["low"] > 0)
        & (out["close"] > 0)
    ]
    out = out[
        (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    ]
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)
