"""多品种海龟策略的简化 K 线回测器。"""

from __future__ import annotations

from dataclasses import dataclass, replace, field
import hashlib
import json
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .data_store import BacktestDataStore
from inv_trend.core.执行约束 import ExecutionPolicy
from inv_trend.domain.walk_forward import BarEvent, order_bar_events
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
    decisions: pd.DataFrame = field(default_factory=pd.DataFrame)
    cash_ledger: pd.DataFrame = field(default_factory=pd.DataFrame)
    carry_cost: float = 0.0
    attribution: pd.DataFrame = field(default_factory=pd.DataFrame)
    open_trade_details: pd.DataFrame = field(default_factory=pd.DataFrame)


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
        take_profit_n: float = 0.0,
        signal_delay_bars: int = 1,
        audit_execution: bool = False,
        execution_policy: ExecutionPolicy | None = None,
        policy_schedule: list[dict] | None = None,
    ) -> None:
        if not np.isfinite(take_profit_n) or take_profit_n < 0:
            raise ValueError("take_profit_n must be finite and non-negative")
        self.take_profit_n = float(take_profit_n)
        self.execution_policy = execution_policy
        self.policy_schedule = sorted(policy_schedule or [], key=lambda x: pd.Timestamp(x["effective_at"]))
        self._event_active = None
        self._known_prices = {}
        self._known_rows = {}
        self._event_time = None
        self._event_phase = None
        self._policy_id = "固定策略"
        self._carry_clock = {}
        self._entry_labels = {}
        self._cumulative_fees = 0.0
        self._cumulative_slippage = 0.0
        self._wf_retiring = set()
        self._margin_retiring = set()
        self._realized_by_symbol = {}
        for item in self.policy_schedule:
            if _as_utc_timestamp(item["trained_through"]) >= _as_utc_timestamp(item["effective_at"]):
                raise ValueError("参数训练截止时间必须严格早于生效时间")

        if isinstance(signal_delay_bars,bool) or not isinstance(signal_delay_bars,int) or signal_delay_bars < 1:
            raise ValueError("signal_delay_bars must be an integer >=1")
        self.signal_delay_bars = signal_delay_bars
        self.audit_execution = bool(audit_execution)
        self.config = config or BacktestConfig()
        self.specs = dict(specs)
        self.rules = _resolve_rules(rules, self.config.rules)
        self.market_data = market_data or BacktestDataStore(data, self.rules)
        self.data = {
            symbol: symbol_data.bars
            for symbol, symbol_data in self.market_data.by_symbol.items()
        }
        missing_specs = set(self.data) - set(self.specs)
        if missing_specs:
            raise ValueError(f"missing asset specifications: {sorted(missing_specs)}")
        # Explicit funding/borrow contracts must never silently become free.
        for symbol, frame in self.data.items():
            for column in (self.specs[symbol].funding_rate_column, self.specs[symbol].borrow_rate_column):
                if column and (column not in frame or not np.isfinite(
                        pd.to_numeric(frame[column],errors="coerce").to_numpy(dtype=float)).all()):
                    raise ValueError(f"{symbol}: missing/non-finite configured carry column {column}")
        self.strategy = strategy or MultiAssetTurtleStrategy(self.specs, self.rules)
        self._initial_policy = (self.rules, dict(self.specs), self.strategy, self.market_data, self.take_profit_n)
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

    def _events(self, dates):
        """唯一事件时钟：旧入口为逻辑开/收盘，新入口要求显式UTC时间。"""
        explicit = self.execution_policy is not None and self.execution_policy.event_clock
        events: list[BarEvent] = []
        previous_ends = {}
        for label in dates:
            for symbol in self.market_data.symbols:
                row = self.market_data.row_at_date(symbol, label)
                if row is None:
                    continue
                if explicit:
                    if "bar_open" not in row or "bar_end" not in row:
                        raise ValueError(f"{symbol}: 缺少明确的bar_open/bar_end执行时间")
                    if pd.Timestamp(row["bar_open"]).tzinfo is None or pd.Timestamp(row["bar_end"]).tzinfo is None:
                        raise ValueError("事件时刻必须携带时区，不可猜测")
                    opened, closed = _as_utc_timestamp(row["bar_open"]), _as_utc_timestamp(row["bar_end"])
                    if pd.isna(opened) or pd.isna(closed):
                        raise ValueError("事件时刻不能缺失")
                    if symbol in previous_ends and opened < previous_ends[symbol]:
                        raise ValueError("同一资产K线区间不能重叠")
                    previous_ends[symbol] = closed
                    if opened >= closed:
                        raise ValueError("K线开盘时间必须早于收盘时间")
                else:
                    opened = closed = label
                # 连续市场相同时间戳按上一Bar收盘→下一Bar开盘的事件序执行。
                for phase, when in (("open", opened), ("close", closed)):
                    events.append(BarEvent(when=when, phase=phase, symbol=symbol, label=label))
        grouped: dict[tuple[Any, str], dict[str, Any]] = {}
        for event in order_bar_events(events, explicit_clock=bool(explicit)):
            grouped.setdefault((event.when, event.phase), {})[event.symbol] = event.label
        for (when, phase), active in grouped.items():
            yield when, phase, active

    def _row_at_event(self, symbol, date):
        if self._event_active is not None:
            label = self._event_active.get(symbol)
            return None if label is None else self.market_data.row_at_date(symbol, label)
        return self.market_data.row_at_date(symbol, date)

    def _label_at_event(self, symbol, date):
        return self._event_active.get(symbol, date) if self._event_active is not None else date

    def _cancel_intents(self, when, reservations, order_rows, reason, *, release=True):
        """撤销与期末未成交都保留原信号身份，不作为成交计费。"""
        for key, intent in list(reservations.intents.items()):
            spec = self.specs[intent.order.symbol]
            order_rows.append({
                **self._order_row(when, intent.order, intent.order.signal_price, 0., spec),
                **self._intent_audit_fields(intent),
                "status": "cancelled" if release else "pending_at_horizon",
                "resolution": reason, "fee_cost": 0., "slippage_cost": 0.,
            })
            if release:
                reservations.release(key)

    def _apply_scheduled_policy(self, when, state, reservations, order_rows):
        while self._schedule_cursor < len(self.policy_schedule):
            item = self.policy_schedule[self._schedule_cursor]
            if when < _as_utc_timestamp(item["effective_at"]):
                break
            # 新训练窗口参数不得重写上一阶段已持有仓位的止损，先等下一真实开盘退出。
            self._wf_retiring.update(state.positions)
            self._cancel_intents(when, reservations, order_rows, "walk_forward_policy_replaced")
            self.rules = TurtleRules(**item["rules"])
            self.specs = dict(item.get("specs", self.specs))
            self.strategy = MultiAssetTurtleStrategy(self.specs, self.rules)
            self.market_data = BacktestDataStore(self._raw_data, self.rules)
            self.take_profit_n = float(item.get("take_profit_n", 0.0))
            self._policy_id = item.get("candidate_id", "锁定参数")
            self._schedule_cursor += 1

    def run(self) -> BacktestResult:
        # 换参会替换规则和数据视图；重复回放必须从原始合同重新开始。
        if self.policy_schedule:
            rules, specs, strategy, market_data, take_profit = self._initial_policy
            self.rules, self.specs, self.strategy = rules, dict(specs), strategy
            self.market_data, self.take_profit_n = market_data, take_profit
        dates = self.market_data.calendar
        if self.evaluation_start is not None:
            dates = [d for d in dates if d >= self.evaluation_start]
        if self.evaluation_end is not None:
            dates = [d for d in dates if d <= self.evaluation_end]
        if not dates:
            raise ValueError("evaluation_start/evaluation_end contain no available market data")
        cash = self.initial_equity
        state = PortfolioState()
        reservations = ReservationBook()
        expiry = ExpiryPolicy()
        equity_points, order_rows, trade_rows, detail_rows, decision_rows, ledger_rows = [], [], [], [], [], []
        carry_total = 0.0
        attribution_rows = []
        bankrupt = False
        self._schedule_cursor = 0
        self._cumulative_fees = self._cumulative_slippage = 0.0
        self._carry_clock, self._entry_labels, self._realized_by_symbol = {}, {}, {}
        self._wf_retiring, self._margin_retiring = set(), set()
        self._policy_id = "固定策略"
        self._known_prices, self._known_rows = {}, {}
        self._raw_data = self.data
        events = list(self._events(dates))
        explicit = self.execution_policy is not None and self.execution_policy.event_clock
        # 只有已经完成的预热数据才能进入初始快照；永不计入仓位和本金。
        first_time = events[0][0]
        for symbol in self.market_data.symbols:
            for row in self.market_data.by_symbol[symbol].records:
                end = _as_utc_timestamp(row["bar_end"]) if explicit else None
                if end is not None and end < first_time:
                    self._known_rows[symbol] = row
                    self._known_prices[symbol] = float(row["close"])
        for i, (when, phase, active) in enumerate(events):
            self._event_active, self._event_time, self._event_phase = active, when, phase
            # 相同时戳的上一Bar收盘属于旧合同；下一次开盘才激活新合同。
            if phase == "open":
                self._apply_scheduled_policy(when, state, reservations, order_rows)
            before_carry = cash
            cash = self._accrue_calendar_cost(when, cash, state)
            carry_total += before_carry - cash
            fill_guard = FillRiskGuard(PortfolioBudgetPolicy(self.rules, self.specs))
            if phase == "open":
                for symbol, label in active.items():
                    row = self.market_data.row_at_date(symbol, label)
                    self._known_prices[symbol] = float(row["open"])
                cash = self._process_intraday_stops(when, cash, state, order_rows, trade_rows, detail_rows, open_only=True)
                boundary = []
                for symbol in sorted((self._wf_retiring | self._margin_retiring) & set(active)):
                    position = state.positions.get(symbol)
                    if position is not None:
                        boundary.append(Order(symbol, "exit", position.side, position.total_qty,
                            "margin_liquidation" if symbol in self._margin_retiring else "walk_forward_rebalance",
                            position.system, self._known_prices[symbol],
                            position.units[-1].n_at_entry))
                cash, _ = self._execute_orders(when, boundary, cash, state, order_rows, trade_rows, detail_rows)
                self._wf_retiring.difference_update(active)
                self._margin_retiring.difference_update(active)
                cash = self._execute_pending_intents(when, cash, state, reservations, fill_guard,
                    expiry, order_rows, trade_rows, detail_rows)
                continue
            cash = self._process_intraday_stops(when, cash, state, order_rows, trade_rows, detail_rows)
            for symbol, label in active.items():
                row = self.market_data.row_at_date(symbol, label)
                self._known_rows[symbol] = row
                self._known_prices[symbol] = float(row["close"])
            before = cash
            cash = self._apply_carry_costs(when, cash, state)
            carry_total += before - cash
            # 仅旧入口保留显式全局终点清算；事件回放使用终点NAV而非末Bar先知清仓。
            last = i == len(events) - 1
            if self.liquidate_at_end and last:
                cash, _ = self._execute_orders(when, self._end_of_data_exit_orders(dates[-1], state),
                    cash, state, order_rows, trade_rows, detail_rows, price_column="close")
            equity = self._mark_equity(when, cash, state)
            if not np.isfinite(cash) or not np.isfinite(equity):
                raise ValueError("non-finite portfolio ledger")
            equity_points.append((when, equity))
            bankrupt = bankrupt or equity <= 0
            gross = self._gross_notional(state)
            cap = self.execution_policy.max_gross_leverage if self.execution_policy else self.rules.max_total_leverage
            margin_call = bool(self.execution_policy and self.execution_policy.enforce_capital and gross>0
                               and equity < .75 * gross / cap)
            if margin_call:
                self._margin_retiring.update(state.positions)
                self._cancel_intents(when, reservations, order_rows, "margin_call")
            for symbol in self.specs:
                position = state.positions.get(symbol)
                unrealized = 0. if position is None else position.unrealized_pnl(
                    self._known_prices[symbol], self.specs[symbol].point_value)-position.entry_cost-position.carry_cost
                attribution_rows.append({"time":when,"symbol":symbol,
                    "realized_pnl":self._realized_by_symbol.get(symbol,0.), "open_net_pnl":unrealized,
                    "total_pnl":self._realized_by_symbol.get(symbol,0.)+unrealized})
            ledger_rows.append({"time": when, "cash": cash, "equity": equity,
                "gross_notional":gross, "margin_reserved":gross/cap, "free_collateral":equity-gross/cap,
                "gross_leverage":gross/equity if equity>0 else 0.,"margin_call":margin_call,
                "fee_cost_cumulative":self._cumulative_fees,"slippage_cost_cumulative":self._cumulative_slippage,
                "open_positions": len(state.positions), "carry_cost_cumulative": carry_total,
                "bankrupt_latched": bankrupt, "candidate_id": self._policy_id})
            if last or bankrupt:
                continue
            snapshots = dict(self._known_rows)
            tradable = set(active) - self._wf_retiring - self._margin_retiring
            dated_generator = getattr(self.strategy, "generate_orders_for_date", None)
            new_orders = (dated_generator(when, snapshots, state, equity, tradable_symbols=tradable)
                if callable(dated_generator) else self.strategy.generate_orders(
                    snapshots, state, equity, tradable_symbols=tradable))
            decision_rows.extend({"time": when, "candidate_id": self._policy_id, **item}
                for item in getattr(self.strategy, "last_decisions", []))
            pending_symbols = {intent.order.symbol for intent in reservations.intents.values()}
            for order in new_orders:
                if order.symbol in pending_symbols:
                    continue
                spec = self.specs[order.symbol]
                label = self._label_at_event(order.symbol, when)
                reservations.reserve(PendingOrderIntent(order=order, created_at=when.isoformat(),
                    signal_bar_time=label.isoformat(),
                    entry_period=self.rules.fast_entry if order.system == "fast" else self.rules.slow_entry,
                    breakout_level=float(order.metadata.get("breakout_level", order.signal_price)),
                    requested_qty=order.qty,
                    reserved_risk=order.risk_1n_pct if order.action in {"open", "add"} else 0.0,
                    reserved_notional=(abs(order.qty * order.signal_price * spec.point_value) / equity
                        if order.action in {"open", "add"} and equity > 0 else 0.0),
                    eligible_from=None, intent_id=_stable_intent_id(when, order)))
        if self.execution_policy is not None:
            self._cancel_intents(events[-1][0], reservations, order_rows, "end_of_available_events", release=False)
        self._event_active = None
        self._event_time = None
        equity_curve = pd.Series([p[1] for p in equity_points], index=[p[0] for p in equity_points], name="equity")
        trades = pd.DataFrame(trade_rows)
        open_details = []
        for symbol, position in state.positions.items():
            mark = self._known_prices.get(symbol, position.avg_entry_price)
            for unit in position.units:
                open_details.append({"symbol": symbol, "side": position.side,
                    "entry_reason": unit.reason, "entry_time": unit.entry_time,
                    "qty": unit.qty, "entry_price": unit.entry_price, "mark_price": mark,
                    "pnl": position.side*unit.qty*(mark-unit.entry_price)*self.specs[symbol].point_value
                        -unit.entry_cost-unit.carry_cost,
                    "mark_time": self._known_rows.get(symbol, {}).get("bar_end"),
                    "status": "OPEN_MARK_TO_MARKET"})
        return BacktestResult(equity_curve=equity_curve, trades=trades, trade_details=pd.DataFrame(detail_rows),
            orders=pd.DataFrame(order_rows), metrics=compute_backtest_metrics(equity_curve, trades),
            open_position_count=len(state.positions), unrealized_pnl=self._terminal_unrealized_pnl(dates[-1], state),
            pending_intent_count=len(reservations.intents), decisions=pd.DataFrame(decision_rows),
            cash_ledger=pd.DataFrame(ledger_rows), carry_cost=carry_total, attribution=pd.DataFrame(attribution_rows),
            open_trade_details=pd.DataFrame(open_details))

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
            row = self._row_at_event(intent.order.symbol, date)
            if row is None:
                continue
            # Delay counts this symbol's observed bars, not other markets' bars.
            indexed = self.market_data.by_symbol[intent.order.symbol]
            signal_time = pd.Timestamp(intent.signal_bar_time)
            if indexed.index.tz is None and signal_time.tzinfo is not None:
                signal_time = signal_time.tz_localize(None)
            elif indexed.index.tz is not None and signal_time.tzinfo is None:
                signal_time = signal_time.tz_localize(indexed.index.tz)
            signal_pos = int(indexed.index.searchsorted(signal_time,side="right"))-1
            current_pos = indexed.positions[self._label_at_event(intent.order.symbol, date)]
            if current_pos-signal_pos < self.signal_delay_bars:
                continue
            fill_price = float(row["open"])
            if not np.isfinite(fill_price) or fill_price <= 0:
                continue
            position = state.positions.get(intent.order.symbol)
            if intent.order.action in {"add", "exit"} and position is None:
                order_rows.append({
                    **self._order_row(date, intent.order, fill_price, 0.0, self.specs[intent.order.symbol]),
                    "status": "cancelled", "resolution": "position no longer exists",
                    **self._intent_audit_fields(intent),
                })
                reservations.release(intent_id)
                continue
            expiry_result = expiry.check_before_fill(
                intent, self.market_data.row_at_previous(intent.order.symbol, self._label_at_event(intent.order.symbol, date)), fill_price
            )
            if expiry_result.status != "pending":
                intent.status = expiry_result.status
                intent.resolution = expiry_result.reason
                intent.resolved_at = date.isoformat()
                order_rows.append({
                    **self._order_row(date, intent.order, fill_price, 0.0, self.specs[intent.order.symbol]),
                    "status": intent.status,
                    "resolution": intent.resolution,
                    **self._intent_audit_fields(intent),
                })
                reservations.release(intent_id)
                continue
            order = intent.order
            if order.action in {"open", "add"}:
                decision = fill_guard.validate(
                    intent, self._execution_price(intent.order, fill_price, self.specs[intent.order.symbol]), state, reservations,
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
                        **self._intent_audit_fields(intent),
                    })
                    reservations.release(intent_id)
                    continue
                intent.approved_qty = decision.approved_qty
                order = replace(order, qty=decision.approved_qty)
            intent.fill_attempts_completed += 1
            before_rows = len(order_rows)
            cash, unfilled = self._execute_orders(
                date, [order], cash, state, order_rows, trade_rows, trade_detail_rows
            )
            for record in order_rows[before_rows:]:
                if self.audit_execution or self.signal_delay_bars != 1:
                    record.update(self._intent_audit_fields(intent))

            if unfilled:
                # Keep the reservation only while the exchange has not supplied
                # a usable opening price; this branch normally cannot occur here.
                continue
            # A cash affordability rejection is not a fill.
            intent.status = (order_rows[-1].get("status", "filled")
                             if len(order_rows) > before_rows else "cancelled")
            intent.resolved_at = date.isoformat()
            reservations.release(intent_id)
        return cash

    def _intent_audit_fields(self, intent: PendingOrderIntent) -> dict:
        fields = {"intent_id":intent.intent_id}
        if self.audit_execution or self.signal_delay_bars != 1:
            fields.update(signal_bar_time=intent.signal_bar_time, signal_delay_bars=self.signal_delay_bars)
            if self.execution_policy is not None:
                fields.update(signal_time=intent.created_at, fill_phase=self._event_phase,
                              candidate_id=intent.order.metadata.get(
                                  "candidate_id", self._policy_id
                              ))
        return fields

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

        if self._event_active is not None:
            return dict(self._known_prices)
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
            if (order.action not in {"open","add","exit"} or order.side not in {LONG,SHORT}
                    or not np.isfinite(order.qty) or order.qty <= 0
                    or (order.action in {"open","add"} and
                        (not np.isfinite(order.n_at_signal) or order.n_at_signal <= 0))):
                raise ValueError("invalid execution order: action, side, quantity or ATR")
            if order.symbol not in self.specs:
                continue
            try:
                fill_price = (
                    float(order.forced_fill_price)
                    if order.action == "exit" and order.forced_fill_price is not None
                    else self.market_data.price(self._label_at_event(order.symbol, date), order.symbol, price_column)
                )
            except KeyError:
                unfilled.append(order)
                continue
            if not np.isfinite(fill_price) or fill_price <= 0:
                unfilled.append(order)
                continue
            spec = self.specs[order.symbol]
            reference_price = fill_price
            fill_price = self._execution_price(order, reference_price, spec)
            if order.action == "exit":
                position = state.positions.get(order.symbol)
                if position is None:
                    continue
                selected_reasons = order.metadata.get("unit_reasons")
                remaining_units = []
                if selected_reasons is not None:
                    remaining_units = [u for u in position.units if u.reason not in selected_reasons]
                    selected_units = [u for u in position.units if u.reason in selected_reasons]
                    if not selected_units:
                        continue
                    position = replace(position, units=selected_units)
                order = replace(order, qty=position.total_qty, side=position.side, system=position.system)
            elif order.action in {"open", "add"}:
                position = state.positions.get(order.symbol)
                if (order.action == "add" and position is None) or (
                    position is not None and (order.action == "open" or position.side != order.side)
                ):
                    raise ValueError("entry action does not match current position")
                if self.execution_policy is not None:
                    rejection = None
                    protective_stop = fill_price-order.side*self.rules.stop_n*order.n_at_signal
                    if not np.isfinite(protective_stop) or protective_stop <= 0:
                        rejection = "non-positive protective stop"
                    if (order.side == LONG and not spec.can_long) or (order.side == SHORT and not spec.can_short):
                        rejection = "asset permission/borrow unavailable"
                    qty = order.qty
                    if self.execution_policy.enforce_capital:
                        prices = self._causal_prices_at_open(date)
                        equity_now = self._mark_equity_at_open(cash, state, prices)
                        cap = min(self.execution_policy.max_gross_leverage, self.rules.max_total_leverage)
                        gross = sum(abs(pos.total_qty * prices.get(sym, pos.avg_entry_price)
                                    * self.specs[sym].point_value) for sym,pos in state.positions.items())
                        fee_unit = fill_price*spec.point_value*spec.cost_bps/10000.
                        slip_unit = abs(fill_price-reference_price)*spec.point_value
                        denominator = reference_price*spec.point_value+cap*(fee_unit+slip_unit)
                        qty = min(qty, _round_down(max(0.,equity_now*cap-gross)/denominator,spec.qty_step))
                    if self.execution_policy.enforce_volume and spec.asset_class == "equity":
                        previous = self.market_data.row_at_previous(order.symbol,self._label_at_event(order.symbol,date))
                        volume = float(previous.get("volume",0.)) if previous else 0.
                        # 只能用此前完整Bar的量，不可用本Bar最终成交量决定开盘可成交数量。
                        if not np.isfinite(volume) or volume <= 0:
                            rejection = "previous completed volume unavailable"
                        else:
                            qty = min(qty,_round_down(volume*self.execution_policy.volume_participation,spec.qty_step))
                    if qty <= 0 or qty < spec.min_qty or qty*fill_price*spec.point_value < spec.min_notional:
                        rejection = rejection or "capital/participation budget exhausted"
                    if rejection:
                        order_rows.append({**self._order_row(date,order,reference_price,0.,spec),
                            "status":"rejected", "resolution":rejection,"fee_cost":0.,"slippage_cost":0.})
                        continue
                    order = replace(order,qty=qty)
                if self.cash_model == "cash" and order.side == LONG:
                    charged_bps = spec.cost_bps if self._price_slippage_enabled() else spec.cost_bps + spec.slippage_bps
                    unit_cost = fill_price * spec.point_value * (1 + charged_bps / 10000)
                    qty = min(order.qty, _round_down(max(cash, 0.) / unit_cost, spec.qty_step))
                    if qty <= 0 or qty < spec.min_qty or qty * fill_price * spec.point_value < spec.min_notional:
                        order_rows.append({
                            **self._order_row(date, order, fill_price, 0., spec),
                            "status": "rejected", "resolution": "insufficient cash including costs",
                        })
                        continue
                    order = replace(order, qty=qty)
            cost = (abs(order.qty * fill_price * spec.point_value) * spec.cost_bps / 10000
                    if self._price_slippage_enabled() else _trade_cost(order.qty, fill_price, spec))
            if self.execution_policy is not None:
                self._cumulative_fees += cost
                self._cumulative_slippage += abs(order.qty * (fill_price-reference_price) * spec.point_value)
            if order.action in {"open", "add"}:
                if self.cash_model == "cash":
                    cash -= order.side * order.qty * fill_price * spec.point_value
                cash -= cost
                self._apply_entry_fill(date, order, fill_price, cost, state)
            elif order.action == "exit":
                # The selected lot subset was frozen above; ordinary exits still close all units.
                pnl = position.unrealized_pnl(fill_price, spec.point_value)
                if self.cash_model == "cash":
                    cash += position.side * position.total_qty * fill_price * spec.point_value
                else:
                    cash += pnl
                cash -= cost
                if position.system == "fast":
                    state.last_fast_trade_won[order.symbol] = pnl - position.entry_cost - cost - position.carry_cost > 0
                if remaining_units:
                    # Removing an earlier stage changes list indices. Preserve each
                    # surviving unit's carry clock instead of charging since entry again.
                    old_units = state.positions[order.symbol].units
                    clocks = {id(unit): self._carry_clock.get(
                        (order.symbol, unit.entry_time, index), unit.entry_time)
                        for index, unit in enumerate(old_units)}
                    for index, unit in enumerate(remaining_units):
                        self._carry_clock[(order.symbol, unit.entry_time, index)] = clocks[id(unit)]
                    state.positions[order.symbol].units = remaining_units
                else:
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
        if order.metadata.get("protective_stop") is False:
            if order.side != LONG:
                raise ValueError("unprotected passive holdings must be long")
            stop = 0.0
        self._entry_labels[(order.symbol, date)] = self._label_at_event(order.symbol, date)
        self._carry_clock[(order.symbol, date)] = date
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
            row = self._row_at_event(symbol, date)
            if row is None:
                continue
            open_price = float(row["open"])
            reason = "intraday_stop"
            first = position.units[0]
            target = first.entry_price + position.side * self.take_profit_n * first.n_at_entry
            # Stop-first for ambiguous same-bar touches. At a gap-open, the
            # observed opening event is known to precede any later range touch.
            if position.side == LONG and (open_price if open_only else float(row["low"])) <= position.stop_price:
                stop_price = min(open_price, position.stop_price) if open_price < position.stop_price else position.stop_price
            elif position.side == SHORT and (open_price if open_only else float(row["high"])) >= position.stop_price:
                stop_price = max(open_price, position.stop_price) if open_price > position.stop_price else position.stop_price
            elif self.take_profit_n > 0 and target > 0 and (
                (position.side == LONG and (open_price if open_only else float(row["high"])) >= target)
                or (position.side == SHORT and (open_price if open_only else float(row["low"])) <= target)
            ):
                stop_price = max(open_price,target) if position.side == LONG else min(open_price,target)
                reason = "intraday_take_profit"
            else:
                continue
            stop_orders.append(
                Order(
                    symbol=symbol,
                    action="exit",
                    side=position.side,
                    qty=position.total_qty,
                    reason=reason,
                    system=position.system,
                    signal_price=stop_price,
                    n_at_signal=position.units[-1].n_at_entry,
                    forced_fill_price=stop_price,
                    metadata={
                        "trigger": "开盘跳空止损" if open_only else reason,
                        "trigger_level": position.stop_price,
                        "trigger_phase": "open" if open_only else "intraday_range",
                        "unit_reasons": [unit.reason for unit in position.units],
                    },
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

    def _price_slippage_enabled(self):
        return self.execution_policy is not None and self.execution_policy.price_slippage

    def _execution_price(self, order, reference, spec):
        if not self._price_slippage_enabled():
            return reference
        direction = order.side if order.action in {"open", "add"} else -order.side
        return reference * (1 + direction * spec.slippage_bps / 10000.)

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
        if self.execution_policy is not None:
            direction = row_side if order.action in {"open", "add"} else -row_side
            reference = fill_price / (1 + direction*spec.slippage_bps/10000.) if self._price_slippage_enabled() else fill_price
            extra = {"reference_price":reference, "fee_cost":cost,
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
        return {
            **extra,
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
            **trace,
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
        self._realized_by_symbol[position.symbol] = self._realized_by_symbol.get(position.symbol,0.) + (
            gross_pnl-position.entry_cost-exit_cost-position.carry_cost)
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

    def _gross_notional(self, state):
        return float(sum(abs(position.total_qty*self._known_prices.get(symbol,position.avg_entry_price)
                         *self.specs[symbol].point_value) for symbol,position in state.positions.items()))

    def _accrue_calendar_cost(self, when, cash, state):
        policy = self.execution_policy
        if policy is None or not policy.charge_calendar_carry:
            return cash
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            rate = policy.financing_bps_per_year/10000.
            if position.side == SHORT:
                rate += policy.short_borrow_bps_per_year/10000.
            mark = self._known_prices.get(symbol,position.avg_entry_price)
            for unit_index, unit in enumerate(position.units):
                key = (symbol,unit.entry_time,unit_index)
                previous = self._carry_clock.get(key,unit.entry_time)
                years = max(0.,(when-pd.Timestamp(previous)).total_seconds())/(365.2425*86400)
                cost = abs(unit.qty*mark*spec.point_value)*rate*years
                unit.carry_cost += cost
                cash -= cost
                self._carry_clock[key] = when
        return cash

    def _apply_carry_costs(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
    ) -> float:
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            row = self._row_at_event(symbol, date)
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
                price = (self._known_prices[symbol] if self._event_active is not None
                         else self.market_data.last_price_on_or_before(date, symbol, "close"))
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
                price = (self._known_prices[symbol] if self._event_active is not None
                         else self.market_data.last_price_on_or_before(date, symbol, "close"))
            except KeyError:
                continue
            total += position.unrealized_pnl(price, spec.point_value)
        return float(total)

    def _end_of_data_exit_orders(
        self,
        date: pd.Timestamp,
        state: PortfolioState,
    ) -> list[Order]:
        # A symbol's last row is unknowable while replaying. Liquidation is an
        # explicit global experiment boundary, never an asset-specific oracle.
        # A closed/halted asset has no executable quote at that boundary: keep
        # it marked at the last observed close and disclose the open position.
        calendar = self.market_data.calendar
        horizon = (calendar[-1] if self.evaluation_end is None else
                   max((t for t in calendar if t <= self.evaluation_end), default=None))
        if date != horizon:
            return []
        orders: list[Order] = []
        for symbol, position in list(state.positions.items()):
            if self.market_data.row_at_date(symbol, date) is None:
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
    if reason == "intraday_take_profit":
        return "take_profit"
    if "stop" in reason:
        return "stop"
    if "exit_" in reason:
        return "trend_exit"
    if reason == "end_of_test":
        return "end_of_test"
    return "other"
