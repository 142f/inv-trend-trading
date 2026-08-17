"""Optional market and execution adapters.

Dependencies are imported lazily so installing the core package does not make
OKX or MetaTrader SDKs mandatory.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_MT5_EXPORTS = frozenset(
    {
        "build_mt5_asset_specs",
        "fetch_mt5_ohlc",
        "fetch_mt5_ohlc_many",
        "list_mt5_symbols",
        "mt5_session",
        "mt5_timeframe",
    }
)
_OKX_EXPORTS = frozenset({"OKXClient", "OKXConfig", "okx_candles_to_frame"})

__all__ = sorted(_MT5_EXPORTS | _OKX_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _MT5_EXPORTS:
        module = import_module(".mt5", __name__)
    elif name in _OKX_EXPORTS:
        try:
            module = import_module(".okx", __name__)
        except ModuleNotFoundError as exc:
            if exc.name in {"okx", "dotenv"}:
                raise ImportError(
                    "OKX integration is optional; install requirements-okx.txt"
                ) from exc
            raise
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
