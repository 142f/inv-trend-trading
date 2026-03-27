from __future__ import annotations

from datetime import datetime

from config.models import Settings
from execution.fills import Fill


def apply_slippage(price: float, is_buy: bool, slippage_bps: float) -> float:
    slip = slippage_bps / 10000.0
    return price * (1 + slip if is_buy else 1 - slip)


def simulate_market_fill(
    settings: Settings,
    timestamp: datetime,
    symbol: str,
    position_side: str,
    action: str,
    ref_price: float,
    quantity: float,
    reason: str,
) -> Fill:
    # position_side: long/short (strategy side), action: open/close/reduce/add
    is_buy = (position_side == "long" and action in {"open", "add"}) or (
        position_side == "short" and action in {"close", "reduce"}
    )
    fill_price = apply_slippage(ref_price, is_buy=is_buy, slippage_bps=settings.cost.slippage_bps)
    notional = fill_price * quantity
    fee = notional * settings.cost.fee_taker
    slippage_cost = abs(fill_price - ref_price) * quantity
    return Fill(
        timestamp=timestamp,
        symbol=symbol,
        side=position_side,
        action=action,
        price=float(fill_price),
        quantity=float(quantity),
        notional=float(notional),
        fee=float(fee),
        slippage_cost=float(slippage_cost),
        reason=reason,
    )
