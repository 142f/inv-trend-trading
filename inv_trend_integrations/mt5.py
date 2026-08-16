"""MT5 adapter port; implementation is intentionally isolated from core logic."""

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Mapping

import pandas as pd

from turtle_multi_asset.models.domain import AssetSpec
from turtle_multi_asset.profiles.asset_profiles import infer_asset_fields as _infer_asset_fields


TIMEFRAME_NAMES = {
    "M1": "TIMEFRAME_M1", "M2": "TIMEFRAME_M2", "M3": "TIMEFRAME_M3",
    "M4": "TIMEFRAME_M4", "M5": "TIMEFRAME_M5", "M6": "TIMEFRAME_M6",
    "M10": "TIMEFRAME_M10", "M12": "TIMEFRAME_M12", "M15": "TIMEFRAME_M15",
    "M20": "TIMEFRAME_M20", "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1",
    "H2": "TIMEFRAME_H2", "H3": "TIMEFRAME_H3", "H4": "TIMEFRAME_H4",
    "H6": "TIMEFRAME_H6", "H8": "TIMEFRAME_H8", "H12": "TIMEFRAME_H12",
    "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1", "MN1": "TIMEFRAME_MN1",
}


def mt5_timeframe(name: str) -> int:
    import MetaTrader5 as mt5

    key = name.upper()
    if key not in TIMEFRAME_NAMES:
        raise ValueError(f"unsupported timeframe {name!r}; valid values: {', '.join(sorted(TIMEFRAME_NAMES))}")
    return int(getattr(mt5, TIMEFRAME_NAMES[key]))


@contextmanager
def mt5_session(
    path: str | None = None, login: int | None = None, password: str | None = None,
    server: str | None = None,
) -> Iterator[object]:
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
    symbol: str, timeframe: str = "D1", start: datetime | str | None = None,
    end: datetime | str | None = None, count: int | None = None,
) -> pd.DataFrame:
    import MetaTrader5 as mt5

    if not mt5.symbol_select(symbol, True):
        code, message = mt5.last_error()
        raise RuntimeError(f"cannot select MT5 symbol {symbol!r}: {code} {message}")
    if start is not None and end is not None:
        rates = mt5.copy_rates_range(symbol, mt5_timeframe(timeframe), _to_utc_datetime(start), _to_utc_datetime(end))
    else:
        bars = 1000 if count is None else int(count)
        if bars <= 0:
            raise ValueError("count must be positive")
        rates = mt5.copy_rates_from_pos(symbol, mt5_timeframe(timeframe), 0, bars)
    if rates is None or len(rates) == 0:
        code, message = mt5.last_error()
        raise RuntimeError(f"no MT5 rates for {symbol!r}: {code} {message}")
    frame = pd.DataFrame(rates)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    frame = frame.set_index("time").sort_index().rename(columns={"tick_volume": "volume"})
    columns = [name for name in ("open", "high", "low", "close", "volume", "spread") if name in frame]
    out = frame[columns].astype(float)
    out.index.name = "time"
    return out


def fetch_mt5_ohlc_many(
    symbols: list[str], timeframe: str = "D1", start: datetime | str | None = None,
    end: datetime | str | None = None, count: int | None = None,
) -> dict[str, pd.DataFrame]:
    return {symbol: fetch_mt5_ohlc(symbol, timeframe, start, end, count) for symbol in symbols}


def list_mt5_symbols(pattern: str = "*", limit: int = 200) -> list[str]:
    import MetaTrader5 as mt5

    symbols = mt5.symbols_get(pattern)
    if symbols is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"cannot list MT5 symbols: {code} {message}")
    return sorted(symbol.name for symbol in symbols)[:limit]


def build_mt5_asset_specs(
    symbols: list[str], overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, AssetSpec]:
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
        inferred = _infer_asset_fields(symbol)
        params = {
            "symbol": symbol, "asset_class": inferred["asset_class"], "cluster": inferred["cluster"],
            "point_value": float(getattr(info, "trade_contract_size", 1.0) or 1.0),
            "qty_step": float(getattr(info, "volume_step", 1.0) or 1.0),
            "min_qty": float(getattr(info, "volume_min", 0.0) or 0.0),
            "can_long": True, "can_short": True, "max_units": inferred["max_units"],
            "unit_1n_risk_pct": inferred["unit_1n_risk_pct"],
            "max_symbol_1n_risk_pct": inferred["max_symbol_1n_risk_pct"],
            "max_symbol_leverage": inferred["max_symbol_leverage"],
            "cost_bps": inferred["cost_bps"], "slippage_bps": inferred["slippage_bps"],
        }
        params.update(dict(overrides.get(symbol, {})))
        specs[symbol] = AssetSpec(**params)
    return specs


def _to_utc_datetime(value: datetime | str) -> datetime:
    result = pd.Timestamp(value).to_pydatetime() if isinstance(value, str) else value
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


__all__ = [
    "build_mt5_asset_specs", "fetch_mt5_ohlc", "fetch_mt5_ohlc_many", "list_mt5_symbols",
    "mt5_session", "mt5_timeframe",
]
