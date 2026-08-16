"""Optional market and execution adapters isolated from strategy use cases.

No CLI imports this package.  The adapters remain opt-in integration ports and
must not be treated as the source of truth for data, risk, or strategy state.
"""

from .mt5 import (
    build_mt5_asset_specs,
    fetch_mt5_ohlc,
    fetch_mt5_ohlc_many,
    list_mt5_symbols,
    mt5_session,
    mt5_timeframe,
)
from .okx import OKXClient, OKXConfig, okx_candles_to_frame

__all__ = [
    "OKXClient", "OKXConfig", "build_mt5_asset_specs", "fetch_mt5_ohlc",
    "fetch_mt5_ohlc_many", "list_mt5_symbols", "mt5_session", "mt5_timeframe",
    "okx_candles_to_frame",
]
