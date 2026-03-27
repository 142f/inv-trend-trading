from __future__ import annotations


def fee_cost(notional: float, fee_rate: float) -> float:
    return notional * fee_rate


def slippage_price(price: float, is_buy: bool, slippage_bps: float) -> float:
    slip = slippage_bps / 10000.0
    return price * (1 + slip if is_buy else 1 - slip)
