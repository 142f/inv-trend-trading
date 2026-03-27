from __future__ import annotations

from data.exchange_client import ExchangeClient


def test_symbol_normalization_for_usdt_contract():
    assert ExchangeClient.normalize_symbol("BTCUSDT") == "BTC/USDT:USDT"
    assert ExchangeClient.normalize_symbol("ETHUSDT") == "ETH/USDT:USDT"
