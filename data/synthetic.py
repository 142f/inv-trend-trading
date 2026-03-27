from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

from data.aggregator import aggregate_daily_frames


def _build_15m_series(start: str, periods: int, seed: int = 7, base_price: float = 30000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, periods=periods, freq="15min", tz="UTC")
    returns = rng.normal(0.0001, 0.003, periods).cumsum()
    close = base_price * np.exp(returns)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0.0001, 0.002, periods))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.0001, 0.002, periods))
    volume = rng.uniform(10, 150, periods)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    df.index.name = "open_ts"
    df["close_ts"] = df.index + pd.Timedelta(minutes=15)
    return df


def _resample(df_15m: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = (
        df_15m.resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    agg.index.name = "open_ts"
    agg["close_ts"] = agg.index + pd.Timedelta(rule)
    return agg


def build_synthetic_symbol_frames(
    symbol: str,
    start: str = "2024-01-01",
    periods_15m: int = 4000,
) -> Dict[str, pd.DataFrame]:
    if "BTC" in symbol:
        price = 35000.0
        seed = 11
    elif "ETH" in symbol:
        price = 1800.0
        seed = 17
    elif "XAU" in symbol or "XAUT" in symbol or "PAXG" in symbol:
        price = 2300.0
        seed = 23
    else:
        price = 100.0
        seed = 29
    f15 = _build_15m_series(start=start, periods=periods_15m, seed=seed, base_price=price)
    frames = {
        "15m": f15,
        "30m": _resample(f15, "30min"),
        "1h": _resample(f15, "1h"),
        "2h": _resample(f15, "2h"),
        "4h": _resample(f15, "4h"),
        "1d": _resample(f15, "1d"),
    }
    return aggregate_daily_frames(frames)


def build_synthetic_dataset(symbols: List[str]) -> Dict[str, Dict[str, pd.DataFrame]]:
    return {s: build_synthetic_symbol_frames(s) for s in symbols}
