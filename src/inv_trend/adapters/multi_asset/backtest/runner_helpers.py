"""Small pure helpers shared by the backtest runner and its projections.

Single responsibility: deterministic intent identity, fill cost arithmetic and
exit-reason classification.  No state, no IO, so they are importable from any
module without creating a cycle.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from ..models.domain import AssetSpec, Order

def _stable_intent_id(date: pd.Timestamp, order: Order) -> str:
    """Create reproducible pending-intent identity from causal order facts."""

    payload = {
        "created_at": date.isoformat(), "symbol": order.symbol,
        "action": order.action, "side": order.side, "qty": order.qty,
        "reason": order.reason, "system": order.system,
        "signal_price": order.signal_price, "n_at_signal": order.n_at_signal,
        "stop_price": order.stop_price, "metadata": dict(order.metadata),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _fill_cash_cost(qty: float, price: float, spec: AssetSpec, *, price_slippage: bool) -> float:
    """Cash charged on a fill. Price slippage stays in the fill price, not here."""

    notional = abs(qty * price * spec.point_value)
    bps = spec.cost_bps + spec.spread_bps
    if not price_slippage:
        bps += spec.slippage_bps
    return notional * bps / 10000.0


def _exit_type(reason: str) -> str:
    if reason == "intraday_take_profit":
        return "take_profit"
    if "stop" in reason:
        return "stop"
    if "exit_" in reason:
        return "trend_exit"
    if reason == "end_of_test":
        return "end_of_test"
    return "other"


__all__ = ["_exit_type", "_fill_cash_cost", "_stable_intent_id"]
