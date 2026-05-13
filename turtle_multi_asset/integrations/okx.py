"""Small OKX V5 client wrapper for data checks and guarded order submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import os
from typing import Any, Iterable, Mapping

import pandas as pd
from dotenv import load_dotenv

from okx.Account import AccountAPI
from okx.MarketData import MarketAPI
from okx.PublicData import PublicAPI
from okx.Trade import TradeAPI


OKX_SUCCESS_CODE = "0"


@dataclass(frozen=True)
class OKXConfig:
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    simulated: bool = True
    enable_trading: bool = False
    domain: str = "https://www.okx.com"
    proxy: str | None = None
    use_server_time: bool = False
    default_swap_inst_id: str = "BTC-USDT-SWAP"
    default_swap_td_mode: str = "cross"
    default_swap_leverage: int = 20
    max_leverage: int = 20

    @property
    def flag(self) -> str:
        return "1" if self.simulated else "0"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = ".env") -> "OKXConfig":
        if env_file:
            load_dotenv(env_file)
        return cls(
            api_key=os.getenv("OKX_API_KEY", "").strip(),
            secret_key=os.getenv("OKX_SECRET_KEY", "").strip(),
            passphrase=os.getenv("OKX_PASSPHRASE", "").strip(),
            simulated=_env_bool("OKX_SIMULATED", default=True),
            enable_trading=_env_bool("OKX_ENABLE_TRADING", default=False),
            domain=os.getenv("OKX_DOMAIN", "https://www.okx.com").strip() or "https://www.okx.com",
            proxy=os.getenv("OKX_PROXY", "").strip() or None,
            use_server_time=_env_bool("OKX_USE_SERVER_TIME", default=False),
            default_swap_inst_id=os.getenv("OKX_DEFAULT_SWAP_INST_ID", "BTC-USDT-SWAP").strip()
            or "BTC-USDT-SWAP",
            default_swap_td_mode=os.getenv("OKX_DEFAULT_SWAP_TD_MODE", "cross").strip() or "cross",
            default_swap_leverage=int(os.getenv("OKX_DEFAULT_SWAP_LEVERAGE", "20")),
            max_leverage=int(os.getenv("OKX_MAX_LEVERAGE", "20")),
        )

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in {
                "OKX_API_KEY": self.api_key,
                "OKX_SECRET_KEY": self.secret_key,
                "OKX_PASSPHRASE": self.passphrase,
            }.items()
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"missing OKX private API credentials: {joined}")


class OKXClient:
    """Convenience wrapper around the official ``python-okx`` SDK.

    Public market data works without credentials. Account and trade methods need
    OKX_API_KEY, OKX_SECRET_KEY and OKX_PASSPHRASE in the environment.
    """

    def __init__(self, config: OKXConfig | None = None) -> None:
        self.config = config or OKXConfig.from_env()
        api_key = self.config.api_key or "-1"
        secret_key = self.config.secret_key or "-1"
        passphrase = self.config.passphrase or "-1"
        common = {
            "api_key": api_key,
            "api_secret_key": secret_key,
            "passphrase": passphrase,
            "use_server_time": self.config.use_server_time,
            "flag": self.config.flag,
            "domain": self.config.domain,
            "proxy": self.config.proxy,
        }
        self.market = MarketAPI(**common)
        self.public = PublicAPI(**common)
        self.account = AccountAPI(**common)
        self.trade = TradeAPI(**common)

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = ".env") -> "OKXClient":
        return cls(OKXConfig.from_env(env_file))

    def system_time(self) -> dict[str, Any]:
        return _ensure_okx_success(self.public.get_system_time())

    def instruments(
        self,
        inst_type: str = "SWAP",
        inst_id: str = "",
        inst_family: str = "",
    ) -> list[dict[str, Any]]:
        response = self.public.get_instruments(
            instType=inst_type,
            instId=inst_id,
            instFamily=inst_family,
        )
        return _data(response)

    def ticker(self, inst_id: str = "BTC-USDT-SWAP") -> dict[str, Any]:
        rows = _data(self.market.get_ticker(instId=inst_id))
        if not rows:
            raise RuntimeError(f"OKX returned no ticker data for {inst_id}")
        return rows[0]

    def orderbook(self, inst_id: str = "BTC-USDT-SWAP", size: int = 5) -> dict[str, Any]:
        rows = _data(self.market.get_orderbook(instId=inst_id, sz=str(size)))
        if not rows:
            raise RuntimeError(f"OKX returned no orderbook data for {inst_id}")
        return rows[0]

    def candles(
        self,
        inst_id: str = "BTC-USDT-SWAP",
        bar: str = "1m",
        limit: int = 100,
        history: bool = False,
    ) -> pd.DataFrame:
        getter = self.market.get_history_candlesticks if history else self.market.get_candlesticks
        rows = _data(getter(instId=inst_id, bar=bar, limit=str(limit)))
        return okx_candles_to_frame(rows, symbol=inst_id, timeframe=bar)

    def balance(self, ccy: str = "") -> list[dict[str, Any]]:
        self.config.require_credentials()
        return _data(self.account.get_account_balance(ccy=ccy))

    def positions(self, inst_type: str = "", inst_id: str = "") -> list[dict[str, Any]]:
        self.config.require_credentials()
        return _data(self.account.get_positions(instType=inst_type, instId=inst_id))

    def set_leverage(
        self,
        inst_id: str,
        lever: str | int,
        margin_mode: str = "cross",
        position_side: str = "",
    ) -> dict[str, Any]:
        self._require_trading_enabled()
        if int(lever) > self.config.max_leverage:
            raise ValueError(f"leverage {lever} exceeds configured max {self.config.max_leverage}")
        response = self.account.set_leverage(
            lever=str(lever),
            mgnMode=margin_mode,
            instId=inst_id,
            posSide=position_side,
        )
        return _ensure_okx_success(response)

    def place_market_order(
        self,
        inst_id: str,
        side: str,
        size: str | int | float | Decimal,
        td_mode: str = "cross",
        client_order_id: str = "",
        position_side: str = "",
        reduce_only: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return self.place_order(
            inst_id=inst_id,
            side=side,
            size=size,
            order_type="market",
            td_mode=td_mode,
            client_order_id=client_order_id,
            position_side=position_side,
            reduce_only=reduce_only,
            dry_run=dry_run,
        )

    def place_limit_order(
        self,
        inst_id: str,
        side: str,
        size: str | int | float | Decimal,
        price: str | int | float | Decimal,
        td_mode: str = "cross",
        client_order_id: str = "",
        position_side: str = "",
        reduce_only: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return self.place_order(
            inst_id=inst_id,
            side=side,
            size=size,
            price=price,
            order_type="limit",
            td_mode=td_mode,
            client_order_id=client_order_id,
            position_side=position_side,
            reduce_only=reduce_only,
            dry_run=dry_run,
        )

    def place_order(
        self,
        inst_id: str,
        side: str,
        size: str | int | float | Decimal,
        order_type: str,
        td_mode: str = "cross",
        price: str | int | float | Decimal | None = None,
        client_order_id: str = "",
        position_side: str = "",
        reduce_only: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        params = _clean_params(
            {
                "instId": inst_id,
                "tdMode": td_mode,
                "side": side.lower(),
                "ordType": order_type.lower(),
                "sz": _number_to_str(size),
                "px": "" if price is None else _number_to_str(price),
                "clOrdId": client_order_id,
                "posSide": position_side,
                "reduceOnly": "true" if reduce_only else "",
            }
        )
        if dry_run:
            return {
                "dry_run": True,
                "simulated": self.config.simulated,
                "would_submit": params,
            }
        self._require_trading_enabled()
        response = self.trade.place_order(
            instId=params["instId"],
            tdMode=params["tdMode"],
            side=params["side"],
            ordType=params["ordType"],
            sz=params["sz"],
            px=params.get("px", ""),
            clOrdId=params.get("clOrdId", ""),
            posSide=params.get("posSide", ""),
            reduceOnly=params.get("reduceOnly", ""),
        )
        return _ensure_okx_success(response)

    def cancel_order(self, inst_id: str, order_id: str = "", client_order_id: str = "") -> dict[str, Any]:
        self._require_trading_enabled()
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id is required")
        response = self.trade.cancel_order(instId=inst_id, ordId=order_id, clOrdId=client_order_id)
        return _ensure_okx_success(response)

    def _require_trading_enabled(self) -> None:
        self.config.require_credentials()
        if not self.config.enable_trading:
            raise RuntimeError("set OKX_ENABLE_TRADING=1 before submitting non-dry-run trade requests")


def okx_candles_to_frame(
    rows: Iterable[Iterable[Any]],
    symbol: str,
    timeframe: str,
) -> pd.DataFrame:
    columns = [
        "timestamp_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "volume_ccy",
        "volume_quote",
        "confirm",
    ]
    rows_list = list(rows)
    width = len(rows_list[0]) if rows_list else len(columns)
    frame = pd.DataFrame(rows_list, columns=columns[:width])
    if frame.empty:
        return pd.DataFrame(
            columns=["time", "open", "high", "low", "close", "volume", "source", "symbol", "timeframe"]
        )
    frame["time"] = pd.to_datetime(pd.to_numeric(frame["timestamp_ms"]), unit="ms", utc=True)
    for column in ["open", "high", "low", "close", "volume", "volume_ccy", "volume_quote"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["source"] = "okx"
    frame["symbol"] = symbol
    frame["timeframe"] = timeframe
    return frame.sort_values("time").reset_index(drop=True)


def _data(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    checked = _ensure_okx_success(response)
    data = checked.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected OKX data payload: {data!r}")
    return data


def _ensure_okx_success(response: Mapping[str, Any]) -> dict[str, Any]:
    code = str(response.get("code", ""))
    if code != OKX_SUCCESS_CODE:
        message = response.get("msg") or response
        data = response.get("data")
        if data:
            message = f"{message}; data={data}"
        raise RuntimeError(f"OKX API error {code}: {message}")
    return dict(response)


def _clean_params(params: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in params.items() if value != ""}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _number_to_str(value: str | int | float | Decimal) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    return format(Decimal(str(value)), "f")


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
