"""Market data validation and quality summaries."""

from __future__ import annotations

import pandas as pd


def validate_ohlcv_frame(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
) -> dict[str, object]:
    duplicate_dates = int(df.duplicated(subset=["date", "symbol"]).sum())
    price_cols = ["open", "high", "low", "close"]
    null_count = int(df[["date", "symbol", *price_cols]].isna().sum().sum())
    non_positive_price_count = int((df[price_cols] <= 0).sum().sum())
    bad_ohlc_count = int(
        (
            (df["high"] < df["low"])
            | (df["open"] > df["high"])
            | (df["open"] < df["low"])
            | (df["close"] > df["high"])
            | (df["close"] < df["low"])
        ).sum()
    )
    dates = pd.to_datetime(df["date"], utc=True).sort_values()
    gaps = dates.diff().dropna()
    return {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "row_count": int(len(df)),
        "start_date": dates.iloc[0] if not dates.empty else "",
        "end_date": dates.iloc[-1] if not dates.empty else "",
        "duplicate_date_count": duplicate_dates,
        "null_count": null_count,
        "non_positive_price_count": non_positive_price_count,
        "bad_ohlc_count": bad_ohlc_count,
        "median_gap_hours": float(gaps.median() / pd.Timedelta(hours=1)) if not gaps.empty else 0.0,
        "max_gap_hours": float(gaps.max() / pd.Timedelta(hours=1)) if not gaps.empty else 0.0,
    }


def validate_alignment(frames: dict[str, pd.DataFrame]) -> dict[str, object]:
    if not frames:
        return {
            "symbol_count": 0,
            "union_date_count": 0,
            "intersection_date_count": 0,
            "alignment_policy": "asset_independent",
        }
    date_sets = {symbol: set(pd.to_datetime(df["date"], utc=True)) for symbol, df in frames.items()}
    union_dates = set().union(*date_sets.values())
    intersection_dates = set.intersection(*date_sets.values()) if date_sets else set()
    return {
        "symbol_count": len(frames),
        "union_date_count": len(union_dates),
        "intersection_date_count": len(intersection_dates),
        "alignment_policy": "asset_independent_no_forward_fill",
        "note": "The backtester uses each asset's own bars and carries last price for closed-market marks; no forward-filled prices are exported.",
    }
