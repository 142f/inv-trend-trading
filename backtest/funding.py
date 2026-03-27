from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from portfolio.state import PositionState


@dataclass
class FundingCharge:
    symbol: str
    timestamp: datetime
    amount: float
    rate: float
    missing_used_zero: bool = False


def is_funding_settlement(ts: datetime, settlement_hours: int) -> bool:
    return ts.hour % settlement_hours == 0 and ts.minute == 0


def calc_funding_charge(
    pos: PositionState,
    mark_price: float,
    ts: datetime,
    funding_rate: Optional[float],
) -> FundingCharge:
    if not pos.is_open:
        return FundingCharge(pos.symbol, ts, 0.0, 0.0, False)
    rate = 0.0 if funding_rate is None else funding_rate
    notional = pos.position_size * mark_price
    # Long pays when rate > 0; short pays when rate < 0 (signed handling)
    signed = rate if pos.side == "long" else -rate
    amount = notional * signed
    return FundingCharge(
        symbol=pos.symbol,
        timestamp=ts,
        amount=amount,
        rate=rate,
        missing_used_zero=(funding_rate is None),
    )


def funding_rate_lookup(
    table: Dict[str, Dict[datetime, float]],
    symbol: str,
    ts: datetime,
) -> Optional[float]:
    sym_table = table.get(symbol, {})
    return sym_table.get(ts)
