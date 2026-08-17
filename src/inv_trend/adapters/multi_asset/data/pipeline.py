"""Modular canonical data processing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from .cleaner import clean_ohlcv_frame
from .loader import load_local_csv
from .normalizer import CANONICAL_COLUMNS, normalize_ohlcv_frame
from .validator import validate_alignment, validate_ohlcv_frame


PRICE_COLUMNS = ["open", "high", "low", "close"]
OPTIONAL_COLUMNS = ["volume", "spread", "source", "timeframe"]


@dataclass(frozen=True)
class DataQualityReport:
    symbol: str
    timeframe: str
    rows: int
    start_date: str
    end_date: str
    missing_values: int
    duplicate_rows: int
    anomaly_rows: int
    non_positive_price_values: int
    median_gap_hours: float
    max_gap_hours: float

    @classmethod
    def from_frame(cls, frame: pd.DataFrame, symbol: str, timeframe: str) -> "DataQualityReport":
        summary = validate_ohlcv_frame(frame, symbol=symbol, timeframe=timeframe)
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            rows=int(summary["row_count"]),
            start_date=str(summary["start_date"]),
            end_date=str(summary["end_date"]),
            missing_values=int(summary["null_count"]),
            duplicate_rows=int(summary["duplicate_date_count"]),
            anomaly_rows=int(summary["bad_ohlc_count"]),
            non_positive_price_values=int(summary["non_positive_price_count"]),
            median_gap_hours=float(summary["median_gap_hours"]),
            max_gap_hours=float(summary["max_gap_hours"]),
        )


def read_data(path: str | Path) -> pd.DataFrame:
    return load_local_csv(path)


def standardize_fields(
    df: pd.DataFrame,
    *,
    symbol: str,
    source: str,
    timeframe: str,
) -> pd.DataFrame:
    return normalize_ohlcv_frame(df, symbol=symbol, source=source, timeframe=timeframe)


def unify_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def inspect_missing_values(df: pd.DataFrame) -> dict[str, int]:
    return {column: int(df[column].isna().sum()) for column in df.columns}


def inspect_duplicates(df: pd.DataFrame) -> dict[str, int]:
    return {
        "duplicate_date_symbol_rows": int(df.duplicated(subset=["date", "symbol"]).sum()),
        "duplicate_full_rows": int(df.duplicated().sum()),
    }


def inspect_price_anomalies(df: pd.DataFrame) -> dict[str, int]:
    non_positive = int((df[PRICE_COLUMNS] <= 0).sum().sum())
    bad_ohlc = int(
        (
            (df["high"] < df["low"])
            | (df["open"] > df["high"])
            | (df["open"] < df["low"])
            | (df["close"] > df["high"])
            | (df["close"] < df["low"])
        ).sum()
    )
    return {
        "non_positive_price_values": non_positive,
        "bad_ohlc_rows": bad_ohlc,
    }


def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    return clean_ohlcv_frame(df)


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    target = timeframe.upper()
    source_tf = str(df["timeframe"].iloc[0]).upper() if not df.empty and "timeframe" in df.columns else target
    if source_tf == target:
        return df.copy()
    if target != "D1":
        raise ValueError(f"unsupported resample target: {target}")

    frames: list[pd.DataFrame] = []
    for symbol, part in df.groupby("symbol", sort=True):
        indexed = part.sort_values("date").set_index("date")
        agg = indexed.resample("1D", label="left", closed="left").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "spread": "mean",
            }
        )
        agg = agg.dropna(subset=PRICE_COLUMNS).reset_index()
        agg["symbol"] = symbol
        agg["source"] = str(part["source"].iloc[0]) if "source" in part.columns else ""
        agg["timeframe"] = target
        frames.append(agg[CANONICAL_COLUMNS])
    if not frames:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def align_assets(frames: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    aligned = {
        symbol: frame.sort_values("date").reset_index(drop=True)
        for symbol, frame in frames.items()
    }
    return aligned, validate_alignment(aligned)


def merge_assets(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)
    merged = pd.concat(frames.values(), ignore_index=True, sort=False)
    keep = [column for column in CANONICAL_COLUMNS if column in merged.columns]
    return merged[keep].sort_values(["symbol", "date"]).reset_index(drop=True)


def validate_data(df: pd.DataFrame, *, symbol: str, timeframe: str) -> DataQualityReport:
    return DataQualityReport.from_frame(df, symbol=symbol, timeframe=timeframe)


def export_backtest_ready(df: pd.DataFrame, path: str | Path) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    return out_path


def quality_report_row(report: DataQualityReport) -> dict[str, object]:
    return {
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "rows": report.rows,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "missing_values": report.missing_values,
        "duplicate_rows": report.duplicate_rows,
        "anomaly_rows": report.anomaly_rows,
        "non_positive_price_values": report.non_positive_price_values,
        "median_gap_hours": report.median_gap_hours,
        "max_gap_hours": report.max_gap_hours,
    }


def data_columns(df: pd.DataFrame) -> list[str]:
    return [column for column in CANONICAL_COLUMNS if column in df.columns] + [
        column for column in df.columns if column not in CANONICAL_COLUMNS
    ]


def ensure_columns(df: pd.DataFrame, extra: Iterable[str] = ()) -> pd.DataFrame:
    out = df.copy()
    for column in [*OPTIONAL_COLUMNS, *extra]:
        if column not in out.columns:
            out[column] = 0.0 if column in {"volume", "spread"} else ""
    return out
