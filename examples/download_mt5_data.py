"""Importable MT5 data download helpers.

The functions in this module are intentionally pure pandas utilities so they
can be tested without a running MetaTrader 5 terminal.
"""

from __future__ import annotations

import pandas as pd


def normalize_ohlc(df: pd.DataFrame, *, symbol: str, timeframe: str) -> pd.DataFrame:
    """Return valid, de-duplicated OHLC rows with symbol metadata."""

    out = df.copy()
    if "time" not in out.columns:
        out = out.reset_index()
    if "time" not in out.columns:
        raise ValueError("input must contain a time column or time index")

    out["time"] = pd.to_datetime(out["time"], utc=True)
    out = out.sort_values("time")
    out = out[~out["time"].duplicated(keep=False)]

    required = ["open", "high", "low", "close"]
    missing = set(required) - set(out.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")
    for column in required:
        out[column] = pd.to_numeric(out[column], errors="coerce")

    valid = (
        out[required].notna().all(axis=1)
        & (out[required] > 0).all(axis=1)
        & (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    )
    out = out.loc[valid].copy()
    out["symbol"] = symbol
    out["timeframe"] = timeframe
    return out.reset_index(drop=True)


def data_quality(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    point: float,
) -> dict[str, float | int | str]:
    """Compute basic gap and spread diagnostics for MT5 OHLC data."""

    out = df.copy()
    if "time" not in out.columns:
        out = out.reset_index()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out = out.sort_values("time").reset_index(drop=True)

    gaps = out["time"].diff().dropna().dt.total_seconds() / 60.0
    expected_gap = _timeframe_minutes(timeframe)
    if not gaps.empty:
        normal_gaps = gaps[gaps <= expected_gap * 1.5]
        if not normal_gaps.empty:
            expected_gap = float(normal_gaps.median())

    large_gaps = gaps[gaps > expected_gap * 1.5]
    market_type = _market_type(symbol)
    normal_session_gap_count = 0
    if market_type == "session":
        normal_session_gap_count = sum(
            _is_normal_session_gap(out.loc[index - 1, "time"], out.loc[index, "time"], expected_gap)
            for index in large_gaps.index
        )

    median_spread = float(pd.to_numeric(out.get("spread", pd.Series(dtype=float)), errors="coerce").median())
    median_spread_bps = 0.0
    if "spread" in out and "close" in out and not out.empty:
        spread_price = pd.to_numeric(out["spread"], errors="coerce") * point
        close = pd.to_numeric(out["close"], errors="coerce")
        median_spread_bps = float((spread_price / close * 10000.0).median())

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bar_count": int(len(out)),
        "market_type": market_type,
        "median_spread": median_spread,
        "median_spread_bps": median_spread_bps,
        "expected_gap_minutes": float(expected_gap),
        "large_gap_count": int(len(large_gaps)),
        "normal_session_gap_count": int(normal_session_gap_count),
        "abnormal_gap_count": int(len(large_gaps) - normal_session_gap_count),
        "max_gap_hours": float(large_gaps.max() / 60.0) if not large_gaps.empty else 0.0,
    }


def _timeframe_minutes(timeframe: str) -> float:
    text = timeframe.strip().upper()
    if text[0].isalpha():
        unit = text[0]
        value = int(text[1:] or "1")
    else:
        unit = text[-1]
        value = int(text[:-1] or "1")
    if unit == "M":
        return float(value)
    if unit == "H":
        return float(value * 60)
    if unit == "D":
        return float(value * 1440)
    raise ValueError(f"unsupported timeframe: {timeframe}")


def _market_type(symbol: str) -> str:
    upper = symbol.upper()
    if "BTC" in upper or "ETH" in upper or "USDT" in upper:
        return "24x7"
    return "session"


def _is_normal_session_gap(previous: pd.Timestamp, current: pd.Timestamp, expected_gap: float) -> bool:
    gap_minutes = (current - previous).total_seconds() / 60.0
    if gap_minutes <= expected_gap * 1.5:
        return False
    return previous.dayofweek == 4 and current.dayofweek in {6, 0}
