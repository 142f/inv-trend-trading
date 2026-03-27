from __future__ import annotations

from enum import IntEnum

import pandas as pd


class TrendState(IntEnum):
    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1


def classify_trend_row(row: pd.Series, prev_row: pd.Series | None) -> TrendState:
    if prev_row is None:
        return TrendState.NEUTRAL
    ema144_up = row["ema144"] > prev_row["ema144"]
    ema169_up = row["ema169"] > prev_row["ema169"]
    ema144_dn = row["ema144"] < prev_row["ema144"]
    ema169_dn = row["ema169"] < prev_row["ema169"]

    bullish = (
        row["close"] > row["ema144"] > row["ema169"]
        and ema144_up
        and ema169_up
    )
    bearish = (
        row["close"] < row["ema144"] < row["ema169"]
        and ema144_dn
        and ema169_dn
    )

    if bullish:
        return TrendState.BULLISH
    if bearish:
        return TrendState.BEARISH
    return TrendState.NEUTRAL


def add_trend_state(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    prev = out.shift(1)
    states = []
    for i in range(len(out)):
        states.append(classify_trend_row(out.iloc[i], prev.iloc[i] if i > 0 else None))
    out["trend_state"] = [int(s) for s in states]
    return out
