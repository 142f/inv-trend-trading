"""Local market data loading helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_local_csv(path: str | Path) -> pd.DataFrame:
    """Load a local OHLCV CSV while preserving all source columns."""

    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"missing data file: {csv_path}")
    return pd.read_csv(csv_path)


def load_backtest_ready_csv(path: str | Path) -> dict[str, pd.DataFrame]:
    """Load a canonical multi-symbol CSV into backtester input frames."""

    df = load_local_csv(path)
    if "date" not in df.columns or "symbol" not in df.columns:
        raise ValueError("backtest-ready CSV must contain date and symbol columns")
    df["date"] = pd.to_datetime(df["date"], utc=True)
    data: dict[str, pd.DataFrame] = {}
    columns = [col for col in ["open", "high", "low", "close", "volume", "spread"] if col in df.columns]
    for symbol, part in df.groupby("symbol", sort=True):
        out = part.sort_values("date").set_index("date")
        out.index.name = "time"
        data[str(symbol)] = out[columns]
    return data
