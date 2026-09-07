"""多品种海龟策略的简化 K 线回测器。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .data_store import BacktestDataStore
from ..models.domain import (
    LONG,
    SHORT,
    AssetSpec,
    Order,
    PortfolioState,
    Position,
    PositionUnit,
    TurtleRules,
)
from ..models.order_intent import PendingOrderIntent, ReservationBook
from ..risk.expiry import ExpiryPolicy
from ..risk.fill_guard import FillRiskGuard
from ..strategy.budget_policy import PortfolioBudgetPolicy
from ..strategy.engine import MultiAssetTurtleStrategy
from ..strategy.sizing import _round_down
from .metrics import compute_backtest_metrics
from ..config import BacktestConfig


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    trade_details: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict[str, float]
    open_position_count: int = 0
    unrealized_pnl: float = 0.0
    pending_intent_count: int = 0


def _resolve_rules(
    rules: TurtleRules | None,
    configured_rules: Mapping[str, Any],
) -> TurtleRules:
    """Build rules from configuration unless the caller supplied a rule set."""

    if rules is not None:
        return rules
    unknown = set(configured_rules) - set(TurtleRules.__dataclass_fields__)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported TurtleRules configuration: {names}")
    return TurtleRules(**dict(configured_rules))


class TurtleBacktester:
    """收盘确认、下一根 K 线开盘成交的研究型回测器。

    该实现用于参数研究和行为验证，不替代交易所级撮合、队列位置和真实订单状态机。
    """

    def __init__(
        self,
        data: Mapping[str, pd.DataFrame],
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules | None = None,
        initial_equity: float | None = None,
        liquidate_at_end: bool | None = None,
        cash_model: str | None = None,
        *,
        config: BacktestConfig | None = None,
        evaluation_start: str | pd.Timestamp | None = None,
        evaluation_end: str | pd.Timestamp | None = None,
        market_data: BacktestDataStore | None = None,
        strategy: Any | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.specs = dict(specs)
        self.rules = _resolve_rules(rules, self.config.rules)
        self.market_data = market_data or BacktestDataStore(data, self.rules)
        self.data = {
            symbol: symbol_data.bars
            for symbol, symbol_data in self.market_data.by_symbol.items()
        }
        self.strategy = strategy or MultiAssetTurtleStrategy(self.specs, self.rules)
        resolved_initial_equity = self.config.initial_equity if initial_equity is None else initial_equity
        self.initial_equity = float(resolved_initial_equity)
        if not np.isfinite(self.initial_equity) or self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        self.liquidate_at_end = (
            self.config.liquidate_at_end if liquidate_at_end is None else liquidate_at_end
        )
        self.cash_model = self.config.cash_model if cash_model is None else cash_model
        if self.cash_model not in {"derivative", "cash"}:
            raise ValueError("cash_model must be 'derivative' or 'cash'")
        self.evaluation_start = (
            None
            if evaluation_start is None
            else _as_utc_timestamp(evaluation_start)
        )
        self.evaluation_end = (
            None if evaluation_end is None else _as_utc_timestamp(evaluation_end)
        )
        if (
            self.evaluation_start is not None
            and self.evaluation_end is not None
            and self.evaluation_end < self.evaluation_start
        ):
            raise ValueError("evaluation_end must not precede evaluation_start")

    def run(self) -> BacktestResult:
        dates = self.market_data.calendar
        if self.evaluation_start is not None:
            dates = [date for date in dates if date >= self.evaluation_start]
            if not dates:
                raise ValueError(
                    "evaluation_start is after the available market data"
                )
        if self.evaluation_end is not None:
            dates = [date for date in dates if date <= self.evaluation_end]
            if not dates:
                raise ValueError("evaluation_end is before the available market data")
        cash = self.initial_equity
        state = PortfolioState()
        reservations = ReservationBook()
        fill_guard = FillRiskGuard(PortfolioBudgetPolicy(self.rules, self.specs))
        expiry = ExpiryPolicy()
        equity_points: list[tuple[pd.Timestamp, float]] = []
        order_rows: list[dict] = []
        trade_rows: list[dict] = []
        trade_detail_rows: list[dict] = []

        for date, snapshots, tradable_symbols in self.market_data.timeline(dates):
            # Existing protective orders have priority when the opening quote
            # has already crossed their stop. Do not inspect this bar's range yet.
            cash = self._process_intraday_stops(
                date, cash, state, order_rows, trade_rows, trade_detail_rows, open_only=True,
            )
            cash = self._execute_pending_intents(
                date,
                cash,
                state,
                reservations,
                fill_guard,
                expiry,
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
            # A signal created on the final evaluation bar has no eligible
            # future open inside this run.  Do not leave invisible reservations
            # behind; terminal equity already contains the complete close.
            if date == dates[-1]:
                continue
            dated_generator = getattr(self.strategy, "generate_orders_for_date", None)
            if callable(dated_generator):
                new_orders = dated_generator(
                    date,
                    snapshots,
                    state,
                    equity,
                    tradable_symbols=tradable_symbols,
                )
            else:
                new_orders = self.strategy.generate_orders(
                    snapshots,
                    state,
                    equity,
                    tradable_symbols=tradable_symbols,
                )
            pending_symbols = {intent.order.symbol for intent in reservations.intents.values()}
            for order in new_orders:
                if order.symbol in pending_symbols:
                    continue
                spec = self.specs[order.symbol]
                reservations.reserve(PendingOrderIntent(
                    order=order,
                    created_at=date.isoformat(),
                    signal_bar_time=date.isoformat(),
                    entry_period=self.rules.fast_entry if order.system == "fast" else self.rules.slow_entry,
                    breakout_level=float(order.metadata.get("breakout_level", order.signal_price)),
                    requested_qty=order.qty,
                    reserved_risk=order.risk_1n_pct if order.action in {"open", "add"} else 0.0,
                    reserved_notional=(
                        abs(order.qty * order.signal_price * spec.point_value) / equity
                        if order.action in {"open", "add"} and equity > 0 else 0.0
                    ),
                    eligible_from=None,
                    intent_id=_stable_intent_id(date, order),
                ))

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
            open_position_count=len(state.positions),
            unrealized_pnl=self._terminal_unrealized_pnl(dates[-1], state),
            pending_intent_count=len(reservations.intents),
        )
    def _execute_pending_intents(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
        reservations: ReservationBook,
        fill_guard: FillRiskGuard,
        expiry: ExpiryPolicy,
        order_rows: list[dict],
        trade_rows: list[dict],
        trade_detail_rows: list[dict],
    ) -> float:
        """Fill at this bar's open, using no current-bar close for risk checks."""
        prices_at_open = self._causal_prices_at_open(date)
        for intent_id, intent in list(reservations.intents.items()):
            row = self.market_data.row_at_date(intent.order.symbol, date)
            if row is None:
                continue
            fill_price = float(row["open"])
            if not np.isfinite(fill_price) or fill_price <= 0:
                continue
            position = state.positions.get(intent.order.symbol)
            if intent.order.action in {"add", "exit"} and position is None:
                order_rows.append({
                    **self._order_row(date, intent.order, fill_price, 0.0, self.specs[intent.order.symbol]),
                    "status": "cancelled", "resolution": "position no longer exists",
                    "intent_id": intent.intent_id,
                })
                reservations.release(intent_id)
                continue
            expiry_result = expiry.check_before_fill(
                intent, self.market_data.row_at_previous(intent.order.symbol, date), fill_price
            )
            if expiry_result.status != "pending":
                intent.status = expiry_result.status
                intent.resolution = expiry_result.reason
                intent.resolved_at = date.isoformat()
                order_rows.append({
                    **self._order_row(date, intent.order, fill_price, 0.0, self.specs[intent.order.symbol]),
                    "status": intent.status,
                    "resolution": intent.resolution,
                    "intent_id": intent.intent_id,
                })
                reservations.release(intent_id)
                continue
            order = intent.order
            if order.action in {"open", "add"}:
                decision = fill_guard.validate(
                    intent, fill_price, state, reservations,
                    self._mark_equity_at_open(cash, state, prices_at_open), prices_at_open,
                )
                if not decision.allowed:
                    intent.status = "rejected"
                    intent.resolution = decision.reason
                    intent.resolved_at = date.isoformat()
                    order_rows.append({
                        **self._order_row(date, order, fill_price, 0.0, self.specs[order.symbol]),
                        "status": intent.status,
                        "resolution": intent.resolution,
                        "intent_id": intent.intent_id,
                    })
                    reservations.release(intent_id)
                    continue
                intent.approved_qty = decision.approved_qty
                order = replace(order, qty=decision.approved_qty)
            intent.fill_attempts_completed += 1
            cash, unfilled = self._execute_orders(
                date, [order], cash, state, order_rows, trade_rows, trade_detail_rows
            )
            if unfilled:
                # Keep the reservation only while the exchange has not supplied
                # a usable opening price; this branch normally cannot occur here.
                continue
            intent.status = "filled"
            intent.resolved_at = date.isoformat()
            reservations.release(intent_id)
        return cash

    def _mark_equity_at_open(
        self,
        cash: float,
        state: PortfolioState,
        prices_at_open: Mapping[str, float],
    ) -> float:
        equity = cash
        for symbol, position in state.positions.items():
            price = prices_at_open.get(symbol)
            spec = self.specs.get(symbol)
            if price is None or spec is None:
                continue
            if self.cash_model == "cash":
                equity += position.market_value(price, spec.point_value)
            else:
                equity += position.unrealized_pnl(price, spec.point_value)
        return float(equity)

    def _causal_prices_at_open(self, date: pd.Timestamp) -> dict[str, float]:
        """Price every asset causally for fill-time portfolio risk checks.

        A symbol trading on ``date`` uses its opening price.  A closed market
        uses the last completed close strictly before ``date`` so an existing
        position cannot disappear from portfolio risk and leverage totals.
        """

        prices: dict[str, float] = {}
        for symbol in self.specs:
            current = self.market_data.row_at_date(symbol, date)
            if current is not None:
                price = float(current["open"])
            else:
                previous = self.market_data.row_at_previous(symbol, date)
                if previous is None:
                    continue
                price = float(previous["close"])
            if np.isfinite(price) and price > 0:
                prices[symbol] = price
        return prices

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
                fill_price = (
                    float(order.forced_fill_price)
                    if order.action == "exit" and order.forced_fill_price is not None
                    else self.market_data.price(date, order.symbol, price_column)
                )
            except KeyError:
                unfilled.append(order)
                continue
            if not np.isfinite(fill_price) or fill_price <= 0:
                unfilled.append(order)
                continue
            spec = self.specs[order.symbol]
            if order.action == "exit":
                position = state.positions.get(order.symbol)
                if position is None:
                    continue
                order = replace(order, qty=position.total_qty, side=position.side, system=position.system)
            elif order.action in {"open", "add"}:
                position = state.positions.get(order.symbol)
                if (order.action == "add" and position is None) or (
                    position is not None and (order.action == "open" or position.side != order.side)
                ):
                    raise ValueError("entry action does not match current position")
                if self.cash_model == "cash" and order.side == LONG:
                    unit_cost = fill_price * spec.point_value * (1 + (spec.cost_bps + spec.slippage_bps) / 10000)
                    qty = min(order.qty, _round_down(max(cash, 0.) / unit_cost, spec.qty_step))
                    if qty <= 0 or qty < spec.min_qty or qty * fill_price * spec.point_value < spec.min_notional:
                        order_rows.append({
                            **self._order_row(date, order, fill_price, 0., spec),
                            "status": "rejected", "resolution": "insufficient cash including costs",
                        })
                        continue
                    order = replace(order, qty=qty)
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
                    state.last_fast_trade_won[order.symbol] = pnl - position.entry_cost - cost - position.carry_cost > 0
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
        *,
        open_only: bool = False,
    ) -> float:
        stop_orders: list[Order] = []
        for symbol, position in list(state.positions.items()):
            row = self.market_data.row_at_date(symbol, date)
            if row is None:
                continue
            open_price = float(row["open"])
            if position.side == LONG and (open_price if open_only else float(row["low"])) <= position.stop_price:
                stop_price = min(open_price, position.stop_price) if open_price < position.stop_price else position.stop_price
            elif position.side == SHORT and (open_price if open_only else float(row["high"])) >= position.stop_price:
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
        return self._execute_orders(
            date,
            stop_orders,
            cash,
            state,
            order_rows,
            trade_rows,
            trade_detail_rows,
        )[0]

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
        net_pnl = gross_pnl - entry_cost - exit_cost - position.carry_cost
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
                    for unit in position.units:
                        unit.carry_cost += position.side * unit.qty * price * spec.point_value * rate
            if (
                position.side == SHORT
                and spec.borrow_rate_column
                and spec.borrow_rate_column in row
            ):
                rate = float(row[spec.borrow_rate_column])
                if np.isfinite(rate):
                    cash -= notional * rate
                    for unit in position.units:
                        unit.carry_cost += unit.qty * price * spec.point_value * rate
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

    def _terminal_unrealized_pnl(
        self,
        date: pd.Timestamp,
        state: PortfolioState,
    ) -> float:
        total = 0.0
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            try:
                price = self.market_data.last_price_on_or_before(date, symbol, "close")
            except KeyError:
                continue
            total += position.unrealized_pnl(price, spec.point_value)
        return float(total)

    def _end_of_data_exit_orders(
        self,
        date: pd.Timestamp,
        state: PortfolioState,
    ) -> list[Order]:
        orders: list[Order] = []
        for symbol, position in list(state.positions.items()):
            symbol_data = self.market_data.by_symbol.get(symbol)
            index = None if symbol_data is None else symbol_data.index
            if index is None:
                continue
            end_position = len(index) - 1 if self.evaluation_end is None else index.searchsorted(self.evaluation_end, side="right") - 1
            if end_position < 0 or date != index[end_position]:
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


def _trade_cost(qty: float, price: float, spec: AssetSpec) -> float:
    notional = abs(qty * price * spec.point_value)
    return notional * (spec.cost_bps + spec.slippage_bps) / 10000.0


def _as_utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _exit_type(reason: str) -> str:
    if "stop" in reason:
        return "stop"
    if "exit_" in reason:
        return "trend_exit"
    if reason == "end_of_test":
        return "end_of_test"
    return "other"
