"""External integrations package."""

from .mt5 import build_mt5_asset_specs, fetch_mt5_ohlc, fetch_mt5_ohlc_many, list_mt5_symbols, mt5_session, mt5_timeframe

try:
    from .okx import OKXClient, OKXConfig
except ModuleNotFoundError as exc:
    if exc.name != "okx":
        raise

    class OKXClient:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise ModuleNotFoundError("未安装 okx SDK，请先安装 requirements-okx.txt")

    class OKXConfig:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise ModuleNotFoundError("未安装 okx SDK，请先安装 requirements-okx.txt")

__all__ = [
    "OKXClient",
    "OKXConfig",
    "build_mt5_asset_specs",
    "fetch_mt5_ohlc",
    "fetch_mt5_ohlc_many",
    "list_mt5_symbols",
    "mt5_session",
    "mt5_timeframe",
]
