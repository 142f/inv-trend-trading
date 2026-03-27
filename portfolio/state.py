from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PositionState:
    symbol: str
    side: str = ""
    entry_price: float = 0.0
    entry_timestamp: Optional[datetime] = None
    stop_price: float = 0.0
    position_size: float = 0.0
    risk_amount: float = 0.0
    entry_grade: str = ""
    r_score_at_entry: float = 0.0
    r_multiple_current: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    added_once: bool = False
    breakeven_active: bool = False
    reduce_stage: int = 0
    force_exit_pending: bool = False

    # Dynamic trailing stop & Timeframe Promotion
    highest_high: float = 0.0
    lowest_low: float = float('inf')
    position_state: int = 1  # 1 = short-term tight tracking, 2 = long-term loose tracking

    # helpers
    initial_stop_distance: float = 0.0
    initial_position_size: float = 0.0
    bars_held: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.position_size > 0 and self.side in {"long", "short"}

    def reset(self):
        symbol = self.symbol
        self.__dict__.update(PositionState(symbol=symbol).__dict__)
