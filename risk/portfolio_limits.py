from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict

from config.models import Settings
from portfolio.state import PositionState


@dataclass
class PortfolioRiskStatus:
    portfolio_risk_ratio: float
    can_open_new: bool
    hard_limit_breached: bool
    target_limit_breached: bool
    daily_dd_hit: bool


def estimate_position_risk_value(pos: PositionState, mark_price: float) -> float:
    if pos.position_size <= 0:
        return 0.0
    if pos.side == "long":
        risk_per_unit = max(mark_price - pos.stop_price, 0.0)
    else:
        risk_per_unit = max(pos.stop_price - mark_price, 0.0)
    return risk_per_unit * pos.position_size


class DailyDrawdownTracker:
    def __init__(self):
        self.current_day: str | None = None
        self.day_start_equity: float = 0.0
        self.day_min_equity: float = 0.0

    def update(self, now: datetime, equity: float):
        day_key = now.strftime("%Y-%m-%d")
        if self.current_day != day_key:
            self.current_day = day_key
            self.day_start_equity = equity
            self.day_min_equity = equity
        self.day_min_equity = min(self.day_min_equity, equity)

    def daily_drawdown(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max((self.day_start_equity - self.day_min_equity) / self.day_start_equity, 0.0)


def evaluate_portfolio_limits(
    settings: Settings,
    equity: float,
    positions: Dict[str, PositionState],
    mark_prices: Dict[str, float],
    daily_dd: float,
) -> PortfolioRiskStatus:
    if equity <= 0:
        return PortfolioRiskStatus(1.0, False, True, True, True)

    total_risk = 0.0
    for symbol, pos in positions.items():
        if not pos.is_open:
            continue
        mark = mark_prices.get(symbol, pos.entry_price)
        total_risk += estimate_position_risk_value(pos, mark)
    risk_ratio = total_risk / equity

    hard = risk_ratio >= settings.risk.portfolio_risk_hard
    target = risk_ratio >= settings.risk.portfolio_risk_target
    dd_hit = daily_dd >= settings.risk.daily_drawdown_limit
    can_open = not hard and not dd_hit

    return PortfolioRiskStatus(
        portfolio_risk_ratio=risk_ratio,
        can_open_new=can_open,
        hard_limit_breached=hard,
        target_limit_breached=target,
        daily_dd_hit=dd_hit,
    )


def apply_correlation_haircut(
    settings: Settings,
    symbol: str,
    side: str,
    risk_pct: float,
    positions: Dict[str, PositionState],
) -> float:
    for sym, pos in positions.items():
        if sym == symbol or not pos.is_open:
            continue
        if pos.side == side:
            return risk_pct * (1.0 - settings.risk.same_direction_corr_haircut)
    return risk_pct
