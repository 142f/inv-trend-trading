"""Order and trade row projections for the multi-asset backtester.

Single responsibility: turn executed orders, positions and completed trades into
the JSON rows the report consumes.  No pricing, sizing or accounting rules live
here; those stay in the runner so the projections can be unit-tested alone.
"""

from __future__ import annotations

import json

import pandas as pd

from ..models.domain import LONG, AssetSpec, Order, Position
from .runner_helpers import _exit_type


class TradeReportingMixin:
    """Row builders mixed into TurtleBacktester."""

    def _order_row(
        self,
        date: pd.Timestamp,
        order: Order,
        fill_price: float,
        cost: float,
        spec: AssetSpec,
        side: int | None = None,
        qty: float | None = None,
        system: str | None = None,
    ) -> dict:
        row_side = order.side if side is None else side
        row_qty = order.qty if qty is None else qty
        row_system = order.system if system is None else system
        extra = {}
        spread_cost = (
            abs(row_qty * fill_price * spec.point_value) * spec.spread_bps / 10000.0 if cost else 0.0
        )
        if self.execution_policy is not None:
            direction = row_side if order.action in {"open", "add"} else -row_side
            reference = fill_price / (1 + direction*spec.slippage_bps/10000.) if self._price_slippage_enabled() else fill_price
            extra = {"reference_price":reference, "fee_cost":cost - spread_cost if cost else 0.,
                "slippage_cost":abs(row_qty*(fill_price-reference)*spec.point_value) if self._price_slippage_enabled() else 0.,
                "fill_phase":self._event_phase,
                "candidate_id":order.metadata.get("candidate_id", self._policy_id),
                "fill_time_precision":"会话开盘或收盘入账；区间触发时刻未知"}
        trace = {}
        if getattr(self.strategy, "emit_order_trace", False):
            trace = {
                "decision_id": order.metadata.get("decision_id"),
                "trigger": order.metadata.get("trigger", order.reason),
                "metadata_json": json.dumps(
                    dict(order.metadata),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            }
        if "execution_resolution" in order.metadata:
            resolution = order.metadata["execution_resolution"]
            extra.update(status="partial" if "scaled" in resolution else "filled",
                         resolution=resolution, requested_qty=order.metadata["requested_qty"])
        return {
            **extra,
            "time": date,
            "symbol": order.symbol,
            "action": order.action,
            "side": row_side,
            "qty": row_qty,
            "fill_price": fill_price,
            "cost": cost,
            **({"spread_cost": spread_cost} if spread_cost else {}),
            "reason": order.reason,
            "system": row_system,
            "risk_1n_pct": order.risk_1n_pct,
            "signal_price": order.signal_price,
            "n_at_signal": order.n_at_signal,
            "stop_price": order.stop_price,
            "notional": abs(row_qty * fill_price * spec.point_value),
            **trace,
        }

    def _trade_row(
        self,
        exit_time: pd.Timestamp,
        order: Order,
        position: Position,
        exit_price: float,
        exit_cost: float,
        gross_pnl: float,
        spec: AssetSpec,
    ) -> dict:
        entry_cost = position.entry_cost
        net_pnl = gross_pnl - entry_cost - exit_cost - position.carry_cost
        entry_time = pd.Timestamp(position.first_entry_time)
        holding_bars = self.market_data.holding_bars(
            position.symbol,
            self._entry_labels.get((position.symbol, entry_time), entry_time),
            self._label_at_event(position.symbol, exit_time),
        )
        return {
            "entry_time": position.first_entry_time,
            "exit_time": exit_time,
            "symbol": position.symbol,
            "system": position.system,
            "side": position.side,
            "side_name": "long" if position.side == LONG else "short",
            "entry_reason": position.entry_reason,
            "exit_reason": order.reason,
            "exit_type": _exit_type(order.reason),
            "unit_count": position.unit_count,
            "add_count": max(position.unit_count - 1, 0),
            "qty": position.total_qty,
            "first_entry_price": position.first_entry_price,
            "avg_entry": position.avg_entry_price,
            "last_add_time": position.last_add_time,
            "last_add_price": position.last_add_price,
            "exit_price": exit_price,
            "initial_stop": position.units[0].stop_price_at_entry if position.units else 0.0,
            "final_stop": position.stop_price,
            "first_n": position.units[0].n_at_entry if position.units else 0.0,
            "last_n": position.units[-1].n_at_entry if position.units else 0.0,
            "entry_cost": entry_cost,
            "exit_cost": exit_cost,
            "total_cost": entry_cost + exit_cost + position.carry_cost,
            **({"carry_cost": position.carry_cost} if position.carry_cost else {}),
            "gross_pnl": gross_pnl,
            "pnl": net_pnl,
            "notional_at_exit": abs(position.total_qty * exit_price * spec.point_value),
            "holding_bars": holding_bars,
        }

    def _trade_detail_rows(
        self,
        exit_time: pd.Timestamp,
        order: Order,
        position: Position,
        exit_price: float,
        exit_cost: float,
        gross_pnl: float,
        spec: AssetSpec,
    ) -> list[dict]:
        trade_net = gross_pnl - position.entry_cost - exit_cost - position.carry_cost
        rows: list[dict] = []
        total_qty = position.total_qty
        for idx, unit in enumerate(position.units, start=1):
            unit_gross = (
                position.side
                * unit.qty
                * (exit_price - unit.entry_price)
                * spec.point_value
            )
            unit_exit_cost = exit_cost * (unit.qty / total_qty) if total_qty else 0.0
            unit_carry_cost = unit.carry_cost
            rows.append(
                {
                    "entry_time": unit.entry_time,
                    "exit_time": exit_time,
                    "symbol": position.symbol,
                    "system": position.system,
                    "side": position.side,
                    "side_name": "long" if position.side == LONG else "short",
                    "unit_index": idx,
                    "unit_count": position.unit_count,
                    "entry_reason": unit.reason,
                    "exit_reason": order.reason,
                    "exit_type": _exit_type(order.reason),
                    "qty": unit.qty,
                    "entry_price": unit.entry_price,
                    "exit_price": exit_price,
                    "n_at_entry": unit.n_at_entry,
                    "stop_at_entry": unit.stop_price_at_entry,
                    "final_stop": position.stop_price,
                    "entry_cost": unit.entry_cost,
                    "allocated_exit_cost": unit_exit_cost,
                    "gross_pnl": unit_gross,
                    "pnl": unit_gross - unit.entry_cost - unit_exit_cost - unit_carry_cost,
                    **({"allocated_carry_cost": unit_carry_cost} if unit_carry_cost else {}),
                    "whole_trade_pnl": trade_net,
                }
            )
        return rows


__all__ = ["TradeReportingMixin"]
