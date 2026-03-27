from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from portfolio.state import PositionState


@dataclass
class AccountState:
    initial_equity: float
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    def equity(self, positions: Dict[str, PositionState], marks: Dict[str, float]) -> float:
        unrealized = 0.0
        for sym, pos in positions.items():
            if not pos.is_open:
                continue
            mark = marks.get(sym, pos.entry_price)
            direction = 1.0 if pos.side == "long" else -1.0
            unrealized += (mark - pos.entry_price) * pos.position_size * direction
        return self.initial_equity + self.realized_pnl - self.fees_paid + unrealized

    def apply_realized(self, pnl: float, fee: float):
        self.realized_pnl += pnl
        self.fees_paid += fee
