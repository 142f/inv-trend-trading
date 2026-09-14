"""Normalize market data column names and dtypes."""

from __future__ import annotations

import pandas as pd


MULTI_ASSET_BAR_COLUMNS = [
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
    return out[MULTI_ASSET_BAR_COLUMNS]


def to_persistent_bars(
    frame: pd.DataFrame,
    instrument,
    *,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """Convert the multi-asset boundary view to the authoritative persisted schema."""
    from inv_trend.data.models import CANONICAL_COLUMNS as PERSISTENT_COLUMNS

    out = frame.copy()
    if "timestamp" not in out:
        if "date" not in out:
            raise ValueError("expected date or timestamp")
        out["timestamp"] = out.pop("date")
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    if "data_source" not in out:
        out["data_source"] = out.pop("source") if "source" in out else instrument.primary_source
    selected_timeframe = (timeframe or (out["timeframe"].iloc[0] if "timeframe" in out and len(out) else "")).upper()
    if not selected_timeframe:
        raise ValueError("timeframe is required")
    deltas = {"D1": "1D", "H4": "4h", "H1": "1h", "W1": "7D"}
    if selected_timeframe not in deltas:
        raise ValueError(f"unsupported timeframe: {selected_timeframe}")
    defaults = {
        "symbol": instrument.symbol, "instrument_id": instrument.instrument_id,
        "asset_class": instrument.asset_class, "market": instrument.market,
        "instrument_type": instrument.instrument_type, "source_symbol": instrument.source_symbol,
        "timeframe": selected_timeframe, "bar_end": out["timestamp"] + pd.Timedelta(deltas[selected_timeframe]),
        "timezone": "UTC", "adjusted_close": out["close"], "currency": instrument.currency,
        "quote_currency": instrument.quote_currency, "price_basis": instrument.price_basis,
        "adjustment_method": instrument.adjustment_method, "is_complete": True,
        "quality_status": "PENDING", "quality_score": float("nan"), "quality_flags": "",
        "raw_file_hash": "", "request_id": "", "raw_snapshot_id": "", "source_run_id": "",
        "dataset_version": "", "curated_version": "", "ingested_at": pd.Timestamp.now(tz="UTC"),
    }
    for name, value in defaults.items():
        if name not in out:
            out[name] = value
    ordered = list(PERSISTENT_COLUMNS) + (["spread"] if "spread" in out else [])
    return out.loc[:, ordered]
