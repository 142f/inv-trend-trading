from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import ccxt

from config.loader import get_env
from config.models import Settings


@dataclass
class SymbolConstraints:
    symbol: str
    min_amount: float
    min_notional: float
    amount_precision: int
    price_precision: int


class ExchangeClient:
    def __init__(self, settings: Settings, authenticated: bool = False):
        self.settings = settings
        self.exchange = self._build_exchange(authenticated=authenticated)
        self.exchange.load_markets()

    def _build_exchange(self, authenticated: bool = False):
        exchange_cls = getattr(ccxt, self.settings.exchange.exchange_id)
        default_type = self._resolve_default_type(
            self.settings.exchange.exchange_id,
            self.settings.exchange.default_type,
        )
        params: Dict[str, Any] = {
            "enableRateLimit": self.settings.exchange.enable_rate_limit,
            "timeout": self.settings.exchange.timeout_ms,
            "options": {"defaultType": default_type},
        }
        if authenticated:
            params["apiKey"] = get_env(self.settings.exchange.api_key_env, "")
            params["secret"] = get_env(self.settings.exchange.api_secret_env, "")
            passphrase = get_env(self.settings.exchange.api_passphrase_env, "")
            if passphrase:
                params["password"] = passphrase
        return exchange_cls(params)

    @staticmethod
    def _resolve_default_type(exchange_id: str, configured: str) -> str:
        if configured:
            return configured
        if exchange_id == "okx":
            return "swap"
        if exchange_id == "binanceusdm":
            return "future"
        return "swap"

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: Optional[int] = None):
        return self.exchange.fetch_ohlcv(
            self.normalize_symbol(symbol),
            timeframe=timeframe,
            since=since,
            limit=limit,
        )

    def fetch_order_book(self, symbol: str, limit: int = 20):
        return self.exchange.fetch_order_book(self.normalize_symbol(symbol), limit=limit)

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            payload = self.exchange.fetch_funding_rate(self.normalize_symbol(symbol))
            return float(payload.get("fundingRate"))
        except Exception:
            return None

    def get_symbol_constraints(self, symbol: str) -> SymbolConstraints:
        market = self.exchange.market(self.normalize_symbol(symbol))
        limits = market.get("limits", {})
        amount_limits = limits.get("amount", {}) if isinstance(limits, dict) else {}
        cost_limits = limits.get("cost", {}) if isinstance(limits, dict) else {}
        precision = market.get("precision", {})
        return SymbolConstraints(
            symbol=symbol,
            min_amount=float(amount_limits.get("min") or 0.0),
            min_notional=float(cost_limits.get("min") or 0.0),
            amount_precision=int(precision.get("amount") or 8),
            price_precision=int(precision.get("price") or 8),
        )

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.exchange.amount_to_precision(self.normalize_symbol(symbol), amount))

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.exchange.price_to_precision(self.normalize_symbol(symbol), price))

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reduce_only: bool = False,
        params: Optional[Dict[str, Any]] = None,
    ):
        params = params or {}
        if reduce_only:
            params["reduceOnly"] = True
        return self.exchange.create_order(
            symbol=self.normalize_symbol(symbol),
            type="market",
            side=side,
            amount=amount,
            params=params,
        )

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        if "/" in symbol:
            return symbol
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}/USDT:USDT"
        return symbol
