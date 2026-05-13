"""Backward-compatible MT5 exports."""

from .integrations.mt5 import (
    _infer_asset_fields,
    _to_utc_datetime,
    build_mt5_asset_specs,
    fetch_mt5_ohlc,
    fetch_mt5_ohlc_many,
    list_mt5_symbols,
    mt5_session,
    mt5_timeframe,
)

__all__ = [
    "_infer_asset_fields",
    "_to_utc_datetime",
    "build_mt5_asset_specs",
    "fetch_mt5_ohlc",
    "fetch_mt5_ohlc_many",
    "list_mt5_symbols",
    "mt5_session",
    "mt5_timeframe",
]
