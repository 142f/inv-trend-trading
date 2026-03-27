from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

import pandas as pd

from data.exchange_client import ExchangeClient
from data.kline_fetcher import ensure_closed_bars, timeframe_to_ms


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_ohlcv_range(
    client: ExchangeClient,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    limit_per_call: int = 500,
    max_pages: int = 200,
    strict_complete: bool = True,
) -> pd.DataFrame:
    since_ms = _to_ms(start)
    end_ms = _to_ms(end)
    tf_ms = timeframe_to_ms(timeframe)

    rows = []
    pages = 0
    while since_ms < end_ms and pages < max_pages:
        pages += 1
        batch = client.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            since=since_ms,
            limit=limit_per_call,
        )
        if not batch:
            break
        rows.extend(batch)
        last_open_ms = int(batch[-1][0])
        # Progress guard against repeated last candle
        next_since = last_open_ms + tf_ms
        if next_since <= since_ms:
            break
        since_ms = next_since

    if strict_complete and since_ms < end_ms and pages >= max_pages:
        raise RuntimeError(
            f"Incomplete OHLCV fetch for {symbol} {timeframe}: "
            f"reached max_pages={max_pages} before end time. "
            f"Increase max_pages or reduce time range."
        )

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])

    df = pd.DataFrame(rows, columns=["open_ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["open_ts"]).sort_values("open_ts")
    df["open_ts"] = pd.to_datetime(df["open_ts"], unit="ms", utc=True)
    df = df.set_index("open_ts")
    df = df.astype(float)

    # strict range cutoff by open time
    end_ts = pd.Timestamp(end)
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")
    df = df[df.index < end_ts]
    df = ensure_closed_bars(df, timeframe=timeframe, now=end)
    return df


def fetch_symbol_frames_range(
    client: ExchangeClient,
    symbol: str,
    raw_timeframes: list[str],
    start: datetime,
    end: datetime,
    limit_per_call: int = 500,
    max_pages: int = 200,
    strict_complete: bool = True,
) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    for tf in raw_timeframes:
        frames[tf] = fetch_ohlcv_range(
            client=client,
            symbol=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit_per_call=limit_per_call,
            max_pages=max_pages,
            strict_complete=strict_complete,
        )
    return frames
