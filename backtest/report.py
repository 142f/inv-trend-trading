from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd

from execution.fills import Fill


@dataclass
class BacktestReport:
    final_equity: float
    total_return: float
    max_drawdown: float
    trade_count: int
    win_rate: float
    avg_pnl_per_trade: float


def build_report(equity_curve: pd.Series, fills: List[Fill], initial_equity: float) -> BacktestReport:
    if equity_curve.empty:
        return BacktestReport(initial_equity, 0.0, 0.0, 0, 0.0, 0.0)
    final_eq = float(equity_curve.iloc[-1])
    total_return = (final_eq - initial_equity) / initial_equity if initial_equity else 0.0
    dd = (equity_curve / equity_curve.cummax() - 1.0).min()

    # Reconstruct completed trades from fills:
    # open/add increase position; reduce/close decrease position.
    # A trade is counted only when position size returns to zero.
    class _TradeState:
        __slots__ = ("qty", "avg_entry", "gross_realized", "fees")

        def __init__(self):
            self.qty = 0.0
            self.avg_entry = 0.0
            self.gross_realized = 0.0
            self.fees = 0.0

    completed_net_pnl: List[float] = []
    states = {}
    ordered = sorted(fills, key=lambda x: x.timestamp)

    for f in ordered:
        key = (f.symbol, f.side)
        st = states.get(key)
        if st is None:
            st = _TradeState()
            states[key] = st

        qty = float(f.quantity)
        price = float(f.price)
        fee = float(f.fee)

        if f.action in {"open", "add"}:
            new_qty = st.qty + qty
            if new_qty > 0:
                st.avg_entry = (st.avg_entry * st.qty + price * qty) / new_qty
                st.qty = new_qty
            st.fees += fee
            continue

        # reduce / close
        if st.qty <= 0:
            # Defensive guard: ignore orphan close events in report metrics.
            continue

        close_qty = min(qty, st.qty)
        if f.side == "long":
            pnl = (price - st.avg_entry) * close_qty
        else:
            pnl = (st.avg_entry - price) * close_qty
        st.gross_realized += pnl

        fee_applied = fee if qty <= 0 else fee * (close_qty / qty)
        st.fees += fee_applied
        st.qty -= close_qty

        if st.qty <= 1e-12:
            completed_net_pnl.append(st.gross_realized - st.fees)
            states[key] = _TradeState()

    wins = [x for x in completed_net_pnl if x > 0]
    win_rate = len(wins) / len(completed_net_pnl) if completed_net_pnl else 0.0
    avg = float(np.mean(completed_net_pnl)) if completed_net_pnl else 0.0
    return BacktestReport(
        final_equity=final_eq,
        total_return=float(total_return),
        max_drawdown=float(abs(dd)),
        trade_count=len(completed_net_pnl),
        win_rate=float(win_rate),
        avg_pnl_per_trade=avg,
    )
