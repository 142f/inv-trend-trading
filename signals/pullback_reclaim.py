from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class PullbackSignal:
    valid: bool
    side: str
    timeframe: str
    trigger_price: float
    signal_close_ts: pd.Timestamp | None


def _reclaim_condition(row: pd.Series, side: str) -> bool:
    has_ema55 = "ema55" in row.index
    ema55 = float(row["ema55"]) if has_ema55 else 0.0
    if side == "long":
        # Check alignment: EMA55 > EMA144/169 baseline
        if has_ema55 and ema55 <= row["base_high"]:
            return False
        return row["low"] <= row["base_high"] and row["close"] > row["ema144"]
    # Check alignment: EMA55 < EMA144/169 baseline
    if has_ema55 and ema55 >= row["base_low"]:
        return False
    return row["high"] >= row["base_low"] and row["close"] < row["ema144"]

def detect_reclaim_at_close(df: pd.DataFrame, close_ts: pd.Timestamp, side: str, timeframe: str) -> PullbackSignal:
    if df.empty or "close_ts" not in df.columns:
        return PullbackSignal(False, side, timeframe, 0.0, None)
    rows = df[df["close_ts"] == close_ts]
    if rows.empty:
        return PullbackSignal(False, side, timeframe, 0.0, None)
    idx = rows.index[-1]
    loc = df.index.get_loc(idx)
    row = df.iloc[loc]
    prev_row = df.iloc[loc - 1] if loc > 0 else None
    curr_ok = _reclaim_condition(row, side)
    prev_ok = _reclaim_condition(prev_row, side) if prev_row is not None else False
    valid = bool(curr_ok and not prev_ok)
    trigger = float(row["high"] if side == "long" else row["low"])
    return PullbackSignal(valid, side, timeframe, trigger, close_ts)
