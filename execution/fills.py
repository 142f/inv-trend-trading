from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Fill:
    timestamp: datetime
    symbol: str
    side: str
    action: str
    price: float
    quantity: float
    notional: float
    fee: float
    slippage_cost: float
    reason: str
