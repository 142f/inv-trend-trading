"""MetaTrader 5 data adapter for Turtle backtests.

The MT5 terminal must be installed, running, and logged in to the target broker
account before these functions can fetch broker-provided historical bars.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Mapping

import pandas as pd

from .strategy import AssetSpec


TIMEFRAME_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M2": "TIMEFRAME_M2",
    "M3": "TIMEFRAME_M3",
    "M4": "TIMEFRAME_M4",
    "M5": "TIMEFRAME_M5",
    "M6": "TIMEFRAME_M6",
    "M10": "TIMEFRAME_M10",
    "M12": "TIMEFRAME_M12",
    "M15": "TIMEFRAME_M15",
    "M20": "TIMEFRAME_M20",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H2": "TIMEFRAME_H2",
    "H3": "TIMEFRAME_H3",
    "H4": "TIMEFRAME_H4",
    "H6": "TIMEFRAME_H6",
    "H8": "TIMEFRAME_H8",
    "H12": "TIMEFRAME_H12",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
    "MN1": "TIMEFRAME_MN1",
}


def mt5_timeframe(name: str) -> int:
    """Return an MT5 timeframe constant from a readable name."""

    import MetaTrader5 as mt5

    key = name.upper()
    if key not in TIMEFRAME_NAMES:
        valid = ", ".join(sorted(TIMEFRAME_NAMES))
        raise ValueError(f"unsupported timeframe {name!r}; valid values: {valid}")
    return int(getattr(mt5, TIMEFRAME_NAMES[key]))


@contextmanager
def mt5_session(
    path: str | None = None,
    login: int | None = None,
    password: str | None = None,
    server: str | None = None,
) -> Iterator[object]:
    """Initialize and shutdown an MT5 session.

    If login credentials are omitted, the function uses the currently logged-in
    terminal account.
    """

    import MetaTrader5 as mt5

    kwargs = {}
    if path:
        kwargs["path"] = path
    if login is not None:
        kwargs["login"] = login
    if password is not None:
        kwargs["password"] = password
    if server is not None:
        kwargs["server"] = server

    if not mt5.initialize(**kwargs):
        code, message = mt5.last_error()
        raise RuntimeError(f"mt5.initialize failed: {code} {message}")
    try:
        yield mt5
    finally:
        mt5.shutdown()


def fetch_mt5_ohlc(
    symbol: str,
    timeframe: str = "D1",
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    count: int | None = None,
) -> pd.DataFrame:
    """Fetch OHLC bars for one symbol from the active MT5 session.

    Use either ``start``/``end`` or ``count``. If both are provided, the date
    range takes precedence.
    """

    import MetaTrader5 as mt5

    if not mt5.symbol_select(symbol, True):
        code, message = mt5.last_error()
        raise RuntimeError(f"cannot select MT5 symbol {symbol!r}: {code} {message}")

    tf = mt5_timeframe(timeframe)
    if start is not None and end is not None:
        rates = mt5.copy_rates_range(
            symbol,
            tf,
            _to_utc_datetime(start),
            _to_utc_datetime(end),
        )
    else:
        bars = 1000 if count is None else int(count)
        if bars <= 0:
            raise ValueError("count must be positive")
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)

    if rates is None or len(rates) == 0:
        code, message = mt5.last_error()
        raise RuntimeError(f"no MT5 rates for {symbol!r}: {code} {message}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    rename = {"tick_volume": "volume"}
    df = df.rename(columns=rename)
    keep = [col for col in ["open", "high", "low", "close", "volume", "spread"] if col in df.columns]
    out = df[keep].astype(float)
    out.index.name = "time"
    return out


def fetch_mt5_ohlc_many(
    symbols: list[str],
    timeframe: str = "D1",
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    count: int | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch OHLC bars for multiple MT5 symbols."""

    return {
        symbol: fetch_mt5_ohlc(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            count=count,
        )
        for symbol in symbols
    }


def list_mt5_symbols(pattern: str = "*", limit: int = 200) -> list[str]:
    """Return broker symbols visible to the active MT5 terminal."""

    import MetaTrader5 as mt5

    symbols = mt5.symbols_get(pattern)
    if symbols is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"cannot list MT5 symbols: {code} {message}")
    names = sorted(symbol.name for symbol in symbols)
    return names[:limit]


def build_mt5_asset_specs(
    symbols: list[str],
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, AssetSpec]:
    """Build AssetSpec objects from MT5 symbol metadata plus overrides."""

    import MetaTrader5 as mt5

    overrides = overrides or {}
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        if not mt5.symbol_select(symbol, True):
            code, message = mt5.last_error()
            raise RuntimeError(f"cannot select MT5 symbol {symbol!r}: {code} {message}")
        info = mt5.symbol_info(symbol)
        if info is None:
            code, message = mt5.last_error()
            raise RuntimeError(f"cannot read MT5 symbol info {symbol!r}: {code} {message}")

        point_value = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
        qty_step = float(getattr(info, "volume_step", 1.0) or 1.0)
        min_qty = float(getattr(info, "volume_min", 0.0) or 0.0)

        inferred = _infer_asset_fields(symbol)
        params = {
            "symbol": symbol,
            "asset_class": inferred["asset_class"],
            "cluster": inferred["cluster"],
            "point_value": point_value,
            "qty_step": qty_step,
            "min_qty": min_qty,
            "can_long": True,
            "can_short": True,
            "max_units": inferred["max_units"],
            "unit_1n_risk_pct": inferred["unit_1n_risk_pct"],
            "max_symbol_1n_risk_pct": inferred["max_symbol_1n_risk_pct"],
            "cost_bps": inferred["cost_bps"],
            "slippage_bps": inferred["slippage_bps"],
        }
        params.update(dict(overrides.get(symbol, {})))
        specs[symbol] = AssetSpec(**params)
    return specs


def _infer_asset_fields(symbol: str) -> dict[str, object]:
    upper = symbol.upper()
    if "XAU" in upper or "GOLD" in upper:
        return {
            "asset_class": "metal",
            "cluster": "precious_metals",
            "max_units": 3,
            "unit_1n_risk_pct": 0.004,
            "max_symbol_1n_risk_pct": 0.016,
            "cost_bps": 1.0,
            "slippage_bps": 3.0,
        }
    if "XAG" in upper or "SILVER" in upper:
        return {
            "asset_class": "metal",
            "cluster": "precious_metals",
            "max_units": 2,
            "unit_1n_risk_pct": 0.003,
            "max_symbol_1n_risk_pct": 0.012,
            "cost_bps": 1.5,
            "slippage_bps": 4.0,
        }
    if "BTC" in upper or "ETH" in upper:
        return {
            "asset_class": "crypto",
            "cluster": "crypto",
            "max_units": 2,
            "unit_1n_risk_pct": 0.003,
            "max_symbol_1n_risk_pct": 0.012,
            "cost_bps": 3.0,
            "slippage_bps": 8.0,
        }
    if upper in {"SPY", "QQQ"} or upper.endswith(".US"):
        return {
            "asset_class": "equity",
            "cluster": "us_equity",
            "max_units": 3,
            "unit_1n_risk_pct": 0.004,
            "max_symbol_1n_risk_pct": 0.016,
            "cost_bps": 1.0,
            "slippage_bps": 3.0,
        }
    return {
        "asset_class": "other",
        "cluster": "other",
        "max_units": 2,
        "unit_1n_risk_pct": 0.003,
        "max_symbol_1n_risk_pct": 0.012,
        "cost_bps": 2.0,
        "slippage_bps": 5.0,
    }


def _to_utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        dt = pd.Timestamp(value).to_pydatetime()
    else:
        dt = value
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
