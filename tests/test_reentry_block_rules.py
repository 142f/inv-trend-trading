from __future__ import annotations

from backtest.engine import REENTRY_BLOCK_REASONS


def test_reentry_block_reasons_contains_force_exit_cases():
    assert "portfolio_risk_or_daily_dd" in REENTRY_BLOCK_REASONS
    assert "1h_flip_and_30m_reverse_cross" in REENTRY_BLOCK_REASONS
    assert "time_stop_under_0.5R" in REENTRY_BLOCK_REASONS
