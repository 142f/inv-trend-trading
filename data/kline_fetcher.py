from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

import pandas as pd

from data.exchange_client import ExchangeClient


TIMEFRAME_TO_MS = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def timeframe_to_ms(timeframe: str) -> int:
    if timeframe in TIMEFRAME_TO_MS:
        return TIMEFRAME_TO_MS[timeframe]
    if timeframe.endswith("m"):
        return int(timeframe[:-1]) * 60 * 1000
    if timeframe.endswith("h"):
        return int(timeframe[:-1]) * 60 * 60 * 1000
    if timeframe.endswith("d"):
        return int(timeframe[:-1]) * 24 * 60 * 60 * 1000
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def _to_df(raw_ohlcv) -> pd.DataFrame:
    df = pd.DataFrame(raw_ohlcv, columns=["open_ts", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["open_ts"] = pd.to_datetime(df["open_ts"], unit="ms", utc=True)
    df = df.set_index("open_ts").sort_index()
    return df.astype(float)


def ensure_closed_bars(df: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    tf_ms = timeframe_to_ms(timeframe)
    close_ts = df.index + pd.to_timedelta(tf_ms, unit="ms")
    mask = close_ts <= pd.Timestamp(now)
    out = df.loc[mask].copy()
    out["close_ts"] = out.index + pd.to_timedelta(tf_ms, unit="ms")
    return out


def fetch_closed_klines(
    client: ExchangeClient,
    symbol: str,
    timeframe: str,
    limit: int,
    now: datetime | None = None,
) -> pd.DataFrame:
    raw = client.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
    df = _to_df(raw)
    return ensure_closed_bars(df, timeframe=timeframe, now=now)


def fetch_symbol_frames(
    client: ExchangeClient,
    symbol: str,
    raw_timeframes: list[str],
    limit: int,
    now: datetime | None = None,
) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    for tf in raw_timeframes:
        frames[tf] = fetch_closed_klines(client, symbol=symbol, timeframe=tf, limit=limit, now=now)
    return frames
