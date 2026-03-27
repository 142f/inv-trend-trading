from __future__ import annotations

from config.models import Settings
from portfolio.state import PositionState
from risk.portfolio_limits import evaluate_portfolio_limits


def test_daily_dd_blocks_new_open():
    settings = Settings()
    pos = PositionState(symbol="BTCUSDT")
    status = evaluate_portfolio_limits(
        settings=settings,
        equity=10000.0,
        positions={"BTCUSDT": pos},
        mark_prices={"BTCUSDT": 30000.0},
        daily_dd=settings.risk.daily_drawdown_limit + 0.001,
    )
    assert status.can_open_new is False
    assert status.daily_dd_hit is True
