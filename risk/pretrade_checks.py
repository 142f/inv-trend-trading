from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from config.models import Settings
from data.exchange_client import SymbolConstraints


@dataclass
class PreTradeResult:
    allowed: bool
    reasons: List[str] = field(default_factory=list)


def _spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid * 10000.0


def check_pretrade(
    settings: Settings,
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    constraints: SymbolConstraints,
    bid: Optional[float],
    ask: Optional[float],
    funding_rate: Optional[float],
    can_open_portfolio: bool,
    symbol_has_position: bool,
) -> PreTradeResult:
    reasons: List[str] = []
    if not can_open_portfolio:
        reasons.append("portfolio_gate_blocked")
    if symbol_has_position:
        reasons.append("symbol_position_exists")
    if qty <= 0:
        reasons.append("non_positive_qty")

    if constraints.min_amount and qty < constraints.min_amount:
        reasons.append("below_min_amount")
    notional = qty * entry_price
    if constraints.min_notional and notional < constraints.min_notional:
        reasons.append("below_min_notional")

    if bid is not None and ask is not None and bid > 0 and ask > 0:
        spread = _spread_bps(bid, ask)
        if spread > settings.cost.spread_threshold_bps:
            reasons.append("spread_too_wide")
    else:
        reasons.append("missing_bid_ask")

    if funding_rate is not None and abs(funding_rate) > settings.cost.funding_extreme_threshold:
        reasons.append("funding_extreme")

    return PreTradeResult(allowed=len(reasons) == 0, reasons=reasons)
