"""Normalize market data column names and dtypes."""

from __future__ import annotations

import pandas as pd


CANONICAL_COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "spread",
    "source",
    "timeframe",
]


def normalize_ohlcv_frame(
    df: pd.DataFrame,
    symbol: str,
    source: str,
    timeframe: str,
) -> pd.DataFrame:
    """Return a canonical OHLCV frame using date/symbol/open/high/low/close."""

    out = df.copy()
    if "date" not in out.columns:
        if "time" in out.columns:
            out = out.rename(columns={"time": "date"})
        elif out.index.name in {"time", "date"}:
            out = out.reset_index().rename(columns={out.index.name: "date"})
        else:
            raise ValueError("expected a date/time column or index")

    out["date"] = pd.to_datetime(out["date"], utc=True)
    for column in ["open", "high", "low", "close"]:
        if column not in out.columns:
            raise ValueError(f"missing required price column: {column}")
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    if "spread" not in out.columns:
        out["spread"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out["spread"] = pd.to_numeric(out["spread"], errors="coerce").fillna(0.0)
    out["symbol"] = symbol
    out["source"] = source
    out["timeframe"] = timeframe.upper()
    return out[CANONICAL_COLUMNS]
