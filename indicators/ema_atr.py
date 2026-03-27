from __future__ import annotations

import pandas as pd


def add_ema(df: pd.DataFrame, span_fast: int = 144, span_slow: int = 169) -> pd.DataFrame:
    out = df.copy()
    out["ema55"] = out["close"].ewm(span=55, adjust=False).mean()
    out["ema144"] = out["close"].ewm(span=span_fast, adjust=False).mean()
    out["ema169"] = out["close"].ewm(span=span_slow, adjust=False).mean()
    out["base_low"] = out[["ema144", "ema169"]].min(axis=1)
    out["base_high"] = out[["ema144", "ema169"]].max(axis=1)
    return out


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder's smoothing
    out["atr14"] = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return out
