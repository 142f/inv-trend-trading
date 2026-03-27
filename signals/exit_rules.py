from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from portfolio.state import PositionState


@dataclass
class ForcedExitDecision:
    force_exit: bool
    reason: str = ""


def reverse_cross_30m(side: str, row_30m: pd.Series) -> bool:
    if row_30m is None or row_30m.empty:
        return False
    if side == "long":
        return bool(row_30m["close"] < row_30m["base_low"])
    return bool(row_30m["close"] > row_30m["base_high"])


def should_force_exit(
    pos: PositionState,
    trend_state_1h: int,
    row_30m: pd.Series | None,
    portfolio_force: bool,
    time_stop_bars: int,
    time_stop_min_r: float,
) -> ForcedExitDecision:
    if portfolio_force:
        return ForcedExitDecision(True, "portfolio_risk_or_daily_dd")

    reversal = (pos.side == "long" and trend_state_1h == -1) or (pos.side == "short" and trend_state_1h == 1)
    if reversal and reverse_cross_30m(pos.side, row_30m):
        return ForcedExitDecision(True, "1h_flip_and_30m_reverse_cross")

    # Time stop-loss is ONLY valid in State 1 (short-term trial). If promoted to State 2, we let profits run.
    if pos.position_state == 1 and pos.bars_held >= time_stop_bars and pos.r_multiple_current < time_stop_min_r:
        return ForcedExitDecision(True, "time_stop_insufficient_r")

    return ForcedExitDecision(False, "")
