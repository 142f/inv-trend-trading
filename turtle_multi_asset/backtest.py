"""多品种海龟策略的简化 K 线回测器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from .backtest_data import BacktestDataStore
from .domain import (
    LONG,
    SHORT,
    AssetSpec,
    Order,
    PortfolioState,
    Position,
    PositionUnit,
    TurtleRules,
)
from .engine import MultiAssetTurtleStrategy
from .metrics import compute_backtest_metrics


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    trade_details: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict[str, float]


class TurtleBacktester:
    """收盘确认、下一根 K 线开盘成交的研究型回测器。

    该实现用于参数研究和行为验证，不替代交易所级撮合、队列位置和真实订单状态机。
    """

    def __init__(
        self,
        data: Mapping[str, pd.DataFrame],
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules | None = None,
        initial_equity: float = 100_000.0,
        liquidate_at_end: bool = True,
        cash_model: str = "derivative",
    ) -> None:
        self.specs = dict(specs)
        self.rules = rules or TurtleRules()
        self.market_data = BacktestDataStore(data, self.rules)
        self.data = {
            symbol: symbol_data.bars
            for symbol, symbol_data in self.market_data.by_symbol.items()
        }
        self.strategy = MultiAssetTurtleStrategy(self.specs, self.rules)
        self.initial_equity = float(initial_equity)
        self.liquidate_at_end = liquidate_at_end
        if cash_model not in {"derivative", "cash"}:
            raise ValueError("cash_model must be 'derivative' or 'cash'")
        self.cash_model = cash_model

    def run(self) -> BacktestResult:
        dates = self.market_data.calendar
        cash = self.initial_equity
        state = PortfolioState()
        pending_orders: list[Order] = []
        equity_points: list[tuple[pd.Timestamp, float]] = []
        order_rows: list[dict] = []
        trade_rows: list[dict] = []
        trade_detail_rows: list[dict] = []

        for date in dates:
            cash, pending_orders = self._execute_orders(
                date,
                pending_orders,
                cash,
                state,
                order_rows,
                trade_rows,
                trade_detail_rows,
            )
            cash = self._process_intraday_stops(
                date,
                cash,
                state,
                order_rows,
                trade_rows,
                trade_detail_rows,
            )
            cash = self._apply_carry_costs(date, cash, state)
            if self.liquidate_at_end:
                cash, _ = self._execute_orders(
                    date,
                    self._end_of_data_exit_orders(date, state),
                    cash,
                    state,
                    order_rows,
                    trade_rows,
                    trade_detail_rows,
                    price_column="close",
                )
            equity = self._mark_equity(date, cash, state)
            equity_points.append((date, equity))
            snapshots = self.market_data.snapshots_through(date)
            new_orders = self.strategy.generate_orders(
                snapshots,
                state,
                equity,
                tradable_symbols=self.market_data.tradable_symbols(date),
            )
            pending_symbols = {order.symbol for order in pending_orders}
            pending_orders.extend(
                order for order in new_orders if order.symbol not in pending_symbols
            )

        equity_curve = pd.Series(
            [point[1] for point in equity_points],
            index=[point[0] for point in equity_points],
            name="equity",
        )
        trades = pd.DataFrame(trade_rows)
        trade_details = pd.DataFrame(trade_detail_rows)
        orders = pd.DataFrame(order_rows)
        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            trade_details=trade_details,
            orders=orders,
            metrics=compute_backtest_metrics(equity_curve, trades),
        )

    def _execute_orders(
        self,
        date: pd.Timestamp,
        orders: list[Order],
        cash: float,
        state: PortfolioState,
        order_rows: list[dict],
        trade_rows: list[dict],
        trade_detail_rows: list[dict],
        price_column: str = "open",
    ) -> tuple[float, list[Order]]:
        unfilled: list[Order] = []
        for order in orders:
            if order.symbol not in self.specs:
                continue
            try:
                fill_price = self.market_data.price(date, order.symbol, price_column)
            except KeyError:
                unfilled.append(order)
                continue
            if not np.isfinite(fill_price) or fill_price <= 0:
                unfilled.append(order)
                continue
            spec = self.specs[order.symbol]
            cost = _trade_cost(order.qty, fill_price, spec)
            if order.action in {"open", "add"}:
                if self.cash_model == "cash":
                    cash -= order.side * order.qty * fill_price * spec.point_value
                cash -= cost
                self._apply_entry_fill(date, order, fill_price, cost, state)
            elif order.action == "exit":
                position = state.positions.get(order.symbol)
                if position is None:
                    continue
                pnl = position.unrealized_pnl(fill_price, spec.point_value)
                if self.cash_model == "cash":
                    cash += position.side * position.total_qty * fill_price * spec.point_value
                else:
                    cash += pnl
                cash -= cost
                if position.system == "fast":
                    state.last_fast_trade_won[order.symbol] = pnl - position.entry_cost - cost > 0
                del state.positions[order.symbol]
                self._record_completed_trade(
                    date,
                    order,
                    position,
                    fill_price,
                    cost,
                    pnl,
                    spec,
                    trade_rows,
                    trade_detail_rows,
                )
            order_rows.append(
                self._order_row(date, order, fill_price, cost, spec)
            )
        return cash, unfilled

    def _apply_entry_fill(
        self,
        date: pd.Timestamp,
        order: Order,
        fill_price: float,
        cost: float,
        state: PortfolioState,
    ) -> None:
        stop = (
            fill_price - self.rules.stop_n * order.n_at_signal
            if order.side == LONG
            else fill_price + self.rules.stop_n * order.n_at_signal
        )
        unit = PositionUnit(
            qty=order.qty,
            entry_price=fill_price,
            n_at_entry=order.n_at_signal,
            entry_time=date,
            reason=order.reason,
            stop_price_at_entry=stop,
            entry_cost=cost,
        )
        position = state.positions.get(order.symbol)
        if position is None:
            state.positions[order.symbol] = Position(
                symbol=order.symbol,
                side=order.side,
                system=order.system,
                units=[unit],
                last_add_price=fill_price,
                stop_price=stop,
            )
            return
        if position.side != order.side:
            raise ValueError("cannot add to opposite-side position")
        position.units.append(unit)
        position.last_add_price = fill_price
        if position.side == LONG:
            position.stop_price = max(position.stop_price, stop)
        else:
            position.stop_price = min(position.stop_price, stop)

    def _process_intraday_stops(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
        order_rows: list[dict],
        trade_rows: list[dict],
        trade_detail_rows: list[dict],
    ) -> float:
        stop_orders: list[Order] = []
        for symbol, position in list(state.positions.items()):
            row = self.market_data.row_at_date(symbol, date)
            if row is None:
                continue
            open_price = float(row["open"])
            if position.side == LONG and float(row["low"]) <= position.stop_price:
                stop_price = min(open_price, position.stop_price) if open_price < position.stop_price else position.stop_price
            elif position.side == SHORT and float(row["high"]) >= position.stop_price:
                stop_price = max(open_price, position.stop_price) if open_price > position.stop_price else position.stop_price
            else:
                continue
            stop_orders.append(
                Order(
                    symbol=symbol,
                    action="exit",
                    side=position.side,
                    qty=position.total_qty,
                    reason="intraday_stop",
                    system=position.system,
                    signal_price=stop_price,
                    n_at_signal=position.units[-1].n_at_entry,
                    forced_fill_price=stop_price,
                )
            )
        if not stop_orders:
            return cash
        return self._execute_stop_orders(
            date,
            stop_orders,
            cash,
            state,
            order_rows,
            trade_rows,
            trade_detail_rows,
        )

    def _execute_stop_orders(
        self,
        date: pd.Timestamp,
        orders: list[Order],
        cash: float,
        state: PortfolioState,
        order_rows: list[dict],
        trade_rows: list[dict],
        trade_detail_rows: list[dict],
    ) -> float:
        for order in orders:
            spec = self.specs[order.symbol]
            position = state.positions.get(order.symbol)
            if position is None:
                continue
            fill_price = (
                float(order.forced_fill_price)
                if order.forced_fill_price is not None
                else float(order.signal_price)
            )
            cost = _trade_cost(position.total_qty, fill_price, spec)
            pnl = position.unrealized_pnl(fill_price, spec.point_value)
            if self.cash_model == "cash":
                cash += position.side * position.total_qty * fill_price * spec.point_value
            else:
                cash += pnl
            cash -= cost
            if position.system == "fast":
                state.last_fast_trade_won[order.symbol] = pnl - position.entry_cost - cost > 0
            del state.positions[order.symbol]
            order_rows.append(
                self._order_row(
                    date,
                    order,
                    fill_price,
                    cost,
                    spec,
                    side=position.side,
                    qty=position.total_qty,
                    system=position.system,
                )
            )
            self._record_completed_trade(
                date,
                order,
                position,
                fill_price,
                cost,
                pnl,
                spec,
                trade_rows,
                trade_detail_rows,
            )
        return cash

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
        return {
            "time": date,
            "symbol": order.symbol,
            "action": order.action,
            "side": row_side,
            "qty": row_qty,
            "fill_price": fill_price,
            "cost": cost,
            "reason": order.reason,
            "system": row_system,
            "risk_1n_pct": order.risk_1n_pct,
            "signal_price": order.signal_price,
            "n_at_signal": order.n_at_signal,
            "stop_price": order.stop_price,
            "notional": abs(row_qty * fill_price * spec.point_value),
        }

    def _record_completed_trade(
        self,
        exit_time: pd.Timestamp,
        order: Order,
        position: Position,
        exit_price: float,
        exit_cost: float,
        gross_pnl: float,
        spec: AssetSpec,
        trade_rows: list[dict],
        trade_detail_rows: list[dict],
    ) -> None:
        trade_rows.append(
            self._trade_row(
                exit_time,
                order,
                position,
                exit_price,
                exit_cost,
                gross_pnl,
                spec,
            )
        )
        trade_detail_rows.extend(
            self._trade_detail_rows(
                exit_time,
                order,
                position,
                exit_price,
                exit_cost,
                gross_pnl,
                spec,
            )
        )

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
        net_pnl = gross_pnl - entry_cost - exit_cost
        entry_time = pd.Timestamp(position.first_entry_time)
        holding_bars = self.market_data.holding_bars(
            position.symbol,
            entry_time,
            exit_time,
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
            "total_cost": entry_cost + exit_cost,
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
        trade_net = gross_pnl - position.entry_cost - exit_cost
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
                    "pnl": unit_gross - unit.entry_cost - unit_exit_cost,
                    "whole_trade_pnl": trade_net,
                }
            )
        return rows

    def _apply_carry_costs(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
    ) -> float:
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            row = self.market_data.row_at_date(symbol, date)
            if row is None:
                continue
            price = float(row["close"])
            notional = abs(position.total_qty * price * spec.point_value)
            if spec.funding_rate_column and spec.funding_rate_column in row:
                rate = float(row[spec.funding_rate_column])
                if np.isfinite(rate):
                    cash -= position.side * notional * rate
            if (
                position.side == SHORT
                and spec.borrow_rate_column
                and spec.borrow_rate_column in row
            ):
                rate = float(row[spec.borrow_rate_column])
                if np.isfinite(rate):
                    cash -= notional * rate
        return cash

    def _mark_equity(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
    ) -> float:
        equity = cash
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            try:
                price = self.market_data.last_price_on_or_before(date, symbol, "close")
            except KeyError:
                continue
            if self.cash_model == "cash":
                equity += position.market_value(price, spec.point_value)
            else:
                equity += position.unrealized_pnl(price, spec.point_value)
        return float(equity)

    def _end_of_data_exit_orders(
        self,
        date: pd.Timestamp,
        state: PortfolioState,
    ) -> list[Order]:
        orders: list[Order] = []
        for symbol, position in list(state.positions.items()):
            symbol_data = self.market_data.by_symbol.get(symbol)
            index = None if symbol_data is None else symbol_data.index
            if index is None or date != index[-1]:
                continue
            orders.append(
                Order(
                    symbol=symbol,
                    action="exit",
                    side=position.side,
                    qty=position.total_qty,
                    reason="end_of_test",
                    system=position.system,
                    signal_price=self.market_data.price(date, symbol, "close"),
                    n_at_signal=position.units[-1].n_at_entry,
                )
            )
        return orders


def _trade_cost(qty: float, price: float, spec: AssetSpec) -> float:
    notional = abs(qty * price * spec.point_value)
    return notional * (spec.cost_bps + spec.slippage_bps) / 10000.0


def _exit_type(reason: str) -> str:
    if "stop" in reason:
        return "stop"
    if "exit_" in reason:
        return "trend_exit"
    if reason == "end_of_test":
        return "end_of_test"
    return "other"


def _metrics(equity_curve: pd.Series, trades: pd.DataFrame) -> dict[str, float]:
    return compute_backtest_metrics(equity_curve, trades)
