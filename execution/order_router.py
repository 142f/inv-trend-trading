from __future__ import annotations

from typing import Optional

from data.exchange_client import ExchangeClient


class LiveOrderRouter:
    def __init__(self, client: ExchangeClient):
        self.client = client

    def market_open(self, symbol: str, side: str, quantity: float):
        order_side = "buy" if side == "long" else "sell"
        return self.client.create_market_order(symbol=symbol, side=order_side, amount=quantity, reduce_only=False)

    def market_close(self, symbol: str, side: str, quantity: float):
        order_side = "sell" if side == "long" else "buy"
        return self.client.create_market_order(symbol=symbol, side=order_side, amount=quantity, reduce_only=True)
