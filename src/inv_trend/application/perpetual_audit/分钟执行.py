"""Minute event replay over the existing Turtle order generator.

No network or live orders. Two explicit OHLC ordering scenarios are retained;
they are scenario outcomes, not mathematically exhaustive intrabar bounds.
"""
from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import math

import numpy as np
import pandas as pd

from inv_trend.adapters.multi_asset.backtest.data_store import BacktestDataStore
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, PortfolioState, Position, PositionUnit, TurtleRules
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.strategy.daily.strategy_checks import prepare_daily_strategy_checks, analyze_prepared_strategy_checks
from inv_trend.core.趋势准入 import trend_filter, confirmed_eligibility
from inv_trend.core.永续风控 import (IsolatedPosition, MarginStateMachine, specification_at,
                                 funding_cashflow, risk_sized_contracts, risk_estimates,
                                 stop_loss_value, volatility_scale)


class MinuteReplay:
    def __init__(self, daily, minutes, marks, funding, specs, candidate, *,
                 initial_equity=100000., scenario='worst_case', start=None, end=None,
                 eligibility_provider=None, slippage_bps=5., risk_mode='standardized'):
        if scenario not in {'worst_case', 'best_case'} or risk_mode not in {'standardized', 'original'}:
            raise ValueError('unknown replay mode')
        self.daily, self.minutes, self.marks, self.funding = daily, minutes, marks, funding
        self.spec_history, self.candidate = specs, candidate
        self.initial_equity, self.scenario = initial_equity, scenario
        self.start, self.end = pd.Timestamp(start), pd.Timestamp(end)
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end < self.start:
            raise ValueError('explicit UTC-aware evaluation boundaries required')
        self.eligibility_provider = eligibility_provider
        if candidate['architecture'] == 'daily' and eligibility_provider is None:
            raise ValueError('daily architecture requires historical eligibility provider, not current snapshots')
        self.slippage = slippage_bps / 10000
        self.risk_mode = risk_mode
        self.state = PortfolioState()
        self.cash = initial_equity
        self.margins, self.active_specs, self.accumulated, self.entry_times = {}, {}, {}, {}
        self.events, self.trades, self.equity, self.ambiguities, self.signals = [], [], [], [], []
        self.machine = MarginStateMachine()
        self.prices = {}
        self.blocked_symbols = set()
        self.last_specs = {}
        self.rules = TurtleRules(fast_entry=candidate['fast_entry'], slow_entry=candidate['slow_entry'],
                                 fast_exit=candidate['fast_exit'], slow_exit=candidate['slow_exit'],
                                 stop_n=candidate['stop_n'], pyramid_step_n=candidate['pyramid_step_n'],
                                 fast_system_enabled=candidate['architecture'] != 'slow',
                                 slow_system_enabled=candidate['architecture'] != 'fast',
                                 max_total_1n_risk_pct=.12, max_total_leverage=3.)
        self.store = BacktestDataStore(daily, self.rules)
        self.daily_checks = {}
        if candidate['architecture'] == 'daily':
            for symbol, frame in daily.items():
                self.daily_checks[symbol] = prepare_daily_strategy_checks(frame, DailyChecksConfig(), session_anchor=frame.index[0])

    def _spec(self, symbol, t):
        spec = specification_at(self.spec_history, symbol, t)
        old = self.active_specs.get(symbol)
        if old and old.contract_value != spec.contract_value:
            raise ValueError('open-position contract conversion requires historical conversion event')
        self.last_specs[symbol] = spec
        return spec

    def _equity(self):
        return self.cash + sum(p.unrealized_pnl(self.prices[s], self.active_specs[s].contract_value)
                               for s, p in self.state.positions.items())

    def _isolated(self, symbol):
        p = self.state.positions[symbol]
        return IsolatedPosition(p.side, p.total_qty, p.avg_entry_price, self.margins[symbol])

    def _close(self, symbol, qty, price, t, reason, *, liquidation=False, margin_override=None):
        p, spec = self.state.positions[symbol], self._spec(symbol, t)
        qty = min(qty, p.total_qty)
        fraction = qty / p.total_qty
        exit_fee = qty * spec.contract_value * price * (spec.liquidation_fee_rate if liquidation else spec.fee_rate + self.slippage)
        gross = p.side * qty * spec.contract_value * (price - p.avg_entry_price)
        entry_cost = sum(u.entry_cost for u in p.units) * fraction
        carry = sum(u.carry_cost for u in p.units) * fraction
        net = gross - exit_fee - entry_cost - carry
        self.cash += gross - exit_fee
        self.accumulated[symbol] += net
        self.events.append(dict(timestamp=str(t), symbol=symbol, action=reason, contracts=qty,
                                price=price, gross_pnl=gross, fee=exit_fee, funding_cost=carry,
                                entry_cost=entry_cost, net_pnl=net, scenario=self.scenario,
                                notional=qty * spec.contract_value * price,
                                spec_status=spec.spec_status))
        if fraction >= 1 - 1e-12:
            pnl = self.accumulated.pop(symbol)
            self.trades.append(dict(symbol=symbol, side=p.side, system=p.system,
                                    entry_time=self.entry_times.pop(symbol), exit_time=str(t),
                                    pnl=pnl, exit_reason=reason))
            if p.system == 'fast':
                self.state.last_fast_trade_won[symbol] = pnl > 0
            del self.state.positions[symbol]
            self.margins.pop(symbol)
            self.active_specs.pop(symbol)
        else:
            for u in p.units:
                u.qty *= 1 - fraction
                u.entry_cost *= 1 - fraction
                u.carry_cost *= 1 - fraction
            self.margins[symbol] = (margin_override if margin_override is not None else
                                    self.margins[symbol] * (1 - fraction))

    def _margin(self, symbol, mark, t):
        if symbol not in self.state.positions:
            return
        spec = self._spec(symbol, t)
        _, events = self.machine.process(self._isolated(symbol), mark, spec)
        for event in events:
            if event['state'] == 'NORMAL':
                continue
            diagnostic = dict(event)
            diagnostic['assessed_fee'] = diagnostic.pop('fee')
            diagnostic['assessed_realized_pnl'] = diagnostic.pop('realized_pnl')
            self.events.append(dict(timestamp=str(t), symbol=symbol, action='MARGIN_STATE', **diagnostic))
            if event['state'] == 'CANCEL_OPEN_ORDERS':
                self.blocked_symbols.add(symbol)
            if event['contracts']:
                margin = self.margins[symbol] + event['realized_pnl'] - event['fee']
                self._close(symbol, event['contracts'], mark, t, event['state'],
                            liquidation=True, margin_override=margin)

    def _fund(self, symbol, row, t):
        if symbol not in self.state.positions:
            return
        spec, p = self._spec(symbol, t), self.state.positions[symbol]
        mark = float(row['mark_price_at_event'])
        flow = funding_cashflow(self._isolated(symbol), float(row['funding_rate']), mark, spec)
        self.cash += flow
        self.margins[symbol] += flow
        for unit in p.units:
            unit.carry_cost -= flow * unit.qty / p.total_qty
        self.events.append(dict(action='FUNDING', symbol=symbol, funding_timestamp=str(t),
                                funding_rate=float(row['funding_rate']), position_size_at_event=p.total_qty,
                                mark_price_at_event=mark, funding_cashflow=flow,
                                source=row.get('source', 'INPUT_EVENT'), timestamp=str(t)))
        self._margin(symbol, mark, t)

    def _risk_usage(self):
        risks, gross = {}, 0.
        for symbol, p in self.state.positions.items():
            spec = self.active_specs[symbol]
            # Budget remaining adverse movement from present price, not initial N.
            risks[symbol] = stop_loss_value(p.side, p.total_qty, self.prices[symbol], p.stop_price,
                                           spec.contract_value, 2 * (spec.fee_rate + self.slippage))
            gross += p.total_qty * spec.contract_value * self.prices[symbol]
        return risks, gross

    def _open(self, order, price, t):
        symbol, side = order.symbol, order.side
        if symbol in self.blocked_symbols:
            self.events.append(dict(timestamp=str(t), symbol=symbol, action='REJECT', reason='LIQUIDATION_CANCELLED_EXPOSURE'))
            return
        spec = self._spec(symbol, t)
        equity = self._equity()
        if equity <= 0:
            return
        if order.action == 'add' and symbol not in self.state.positions:
            return
        if symbol in self.state.positions and self.state.positions[symbol].side != side:
            self._close(symbol, self.state.positions[symbol].total_qty, price, t, 'REVERSE_CLOSE')
        stop = price - side * self.rules.stop_n * order.n_at_signal
        risks, gross = self._risk_usage()
        effective_spec = replace(spec, fee_rate=spec.fee_rate + self.slippage)
        qty = risk_sized_contracts(equity=equity, side=side, price=price, stop=stop, spec=effective_spec,
                                  portfolio_stop_used=sum(risks.values()), symbol_stop_used=risks.get(symbol, 0.),
                                  gross_notional=gross, margin_used=sum(max(m, 0) for m in self.margins.values()))
        if self.risk_mode == 'original':
            unit_notional = price * spec.contract_value
            qty = min(order.qty, max(0., 3 * equity - gross) / unit_notional,
                      spec.tiers[-1].notional_cap / unit_notional)
            qty = math.floor(qty / spec.lot_size) * spec.lot_size
        if not self.candidate['pyramid'] and order.action == 'add':
            qty = 0.
        if qty and self.risk_mode == 'standardized':
            prices = pd.DataFrame({s: f.close for s, f in self.daily.items()}).loc[:t.normalize() - pd.Timedelta(days=1)]
            cov, stress = risk_estimates(prices, covariance_days=self.candidate['covariance_days'])
            symbols = list(prices.columns)
            weights = np.array([self.state.positions[s].side * self.state.positions[s].total_qty *
                                self.active_specs[s].contract_value * self.prices[s] / equity
                                if s in self.state.positions else 0. for s in symbols])
            # Bisection sizes the incremental order without assuming a hedge is safe.
            low, high = 0., qty
            for _ in range(40):
                mid = (low + high) / 2
                proposal = weights.copy()
                proposal[symbols.index(symbol)] += side * mid * spec.contract_value * price / equity
                if volatility_scale(proposal, cov, stress) >= 1 - 1e-12:
                    low = mid
                else:
                    high = mid
            qty = math.floor(low / spec.lot_size) * spec.lot_size
        if qty < spec.minimum_size:
            self.events.append(dict(timestamp=str(t), symbol=symbol, action='REJECT', reason='RISK_OR_MINIMUM_SIZE'))
            return
        notional = qty * price * spec.contract_value
        existing = self.state.positions.get(symbol)
        total_qty = qty + (existing.total_qty if existing else 0.)
        leverage = spec.leverage(total_qty * price * spec.contract_value)
        required_margin = total_qty * price * spec.contract_value / leverage
        additional_margin = max(0., required_margin - self.margins.get(symbol, 0.))
        fee = notional * (spec.fee_rate + self.slippage)
        if sum(self.margins.values()) + additional_margin + fee > .8 * equity:
            self.events.append(dict(timestamp=str(t), symbol=symbol, action='REJECT', reason='POST_TIER_MARGIN'))
            return
        self.cash -= fee
        unit = PositionUnit(qty, price, order.n_at_signal, str(t), order.reason, stop, fee)
        if existing:
            existing.units.append(unit)
            existing.last_add_price = price
            existing.stop_price = max(existing.stop_price, stop) if side == 1 else min(existing.stop_price, stop)
        else:
            self.state.positions[symbol] = Position(symbol, side, order.system, [unit], price, stop)
            self.accumulated[symbol] = 0.
            self.entry_times[symbol] = str(t)
        self.margins[symbol] = self.margins.get(symbol, 0.) + additional_margin
        self.active_specs[symbol] = spec
        self.events.append(dict(timestamp=str(t), symbol=symbol, action=order.action.upper(),
                                contracts=qty, price=price, fee=fee, stop=stop, leverage=leverage,
                                notional=notional,
                                spec_status=spec.spec_status))

    def _orders(self, t):
        date = t.normalize() - pd.Timedelta(days=1)
        rows = {s: self.store.row_at_date(s, date) for s in self.daily if date in self.daily[s].index}
        specs = {s: AssetSpec(s, 'crypto', 'crypto', point_value=self._spec(s, t).contract_value,
                             qty_step=self._spec(s, t).lot_size, min_qty=self._spec(s, t).minimum_size,
                             max_units=3 if self.candidate['pyramid'] else 1,
                             unit_1n_risk_pct=.003, max_symbol_leverage=3.) for s in rows}
        strategy = MultiAssetTurtleStrategy(specs, self.rules)
        orders = strategy.generate_orders(rows, self.state, self._equity())
        accepted = []
        for order in orders:
            allowed, reason = True, 'BASE_RULE'
            if order.action == 'open':
                closes = self.daily[order.symbol].loc[:date, 'close']
                architecture = self.candidate['architecture']
                if architecture == 'ma':
                    allowed = trend_filter(order.side, float(closes.iloc[-1]), float(closes.tail(self.candidate['ma_period']).mean()))
                    reason = 'LONG_MA'
                elif architecture == 'momentum':
                    period = self.candidate['momentum_period']
                    allowed = len(closes) > period and trend_filter(order.side, float(closes.iloc[-1]), float(closes.iloc[-period - 1]))
                    reason = 'TIME_SERIES_MOMENTUM'
                elif architecture == 'daily':
                    prepared = self.daily_checks[order.symbol]
                    pos = prepared.base.index.get_loc(date)
                    checks = analyze_prepared_strategy_checks(prepared, position=pos)
                    rating = checks['rating']
                    eligibility = self.eligibility_provider(order.symbol, date, self.state, self._equity(),
                        order=order, bars=self.store.by_symbol[order.symbol].bars,
                        cash=self.cash - sum(self.margins.values()), equity_history=self.equity)
                    allowed = (rating.get('grade') == 'A' and rating.get('direction') == ('long' if order.side == 1 else 'short') and
                               not any(v == 'conflict' for v in rating.get('family_votes', {}).values()) and
                               confirmed_eligibility(eligibility))
                    reason = 'DAILY_GRADE_AND_HISTORICAL_ELIGIBILITY'
            self.signals.append(dict(timestamp=str(date), symbol=order.symbol, action=order.action,
                                     allowed=bool(allowed), reason=reason))
            if allowed:
                accepted.append(order)
        return accepted

    def _rebalance(self, t):
        if not self.state.positions or self.risk_mode != 'standardized':
            return
        equity = self._equity()
        if equity <= 0:
            return
        closes = pd.DataFrame({s: f.close for s, f in self.daily.items()}).loc[:t.normalize() - pd.Timedelta(days=1)]
        cov, stress = risk_estimates(closes, covariance_days=self.candidate['covariance_days'])
        risks, gross = self._risk_usage()
        weights = [self.state.positions[s].side * self.state.positions[s].total_qty *
                   self.active_specs[s].contract_value * self.prices[s] / equity
                   if s in self.state.positions else 0. for s in closes.columns]
        factor = min(volatility_scale(weights, cov, stress),
                     .02 * equity / sum(risks.values()) if sum(risks.values()) else 1.,
                     3 * equity / gross if gross else 1.,
                     .8 * equity / sum(self.margins.values()) if sum(self.margins.values()) > 0 else 1.)
        for s in list(self.state.positions):
            p, spec = self.state.positions[s], self._spec(s, t)
            local = min(factor, .01 * equity / risks[s] if risks[s] else 1.)
            # Current historical tier can reduce the permissible leverage.
            leverage = spec.leverage(p.total_qty * spec.contract_value * self.prices[s])
            local = min(local, max(0., self.margins[s]) * leverage /
                        (p.total_qty * spec.contract_value * self.prices[s]))
            target = math.floor(p.total_qty * local / spec.lot_size) * spec.lot_size
            if target < spec.minimum_size:
                target = 0.
            if p.total_qty - target >= spec.lot_size * .999:
                self._close(s, p.total_qty - target, self.prices[s], t, 'RISK_REDUCTION')

    def _execute_orders(self, orders, t):
        for order in orders:
            if order.action == 'exit':
                if order.symbol in self.state.positions:
                    self._close(order.symbol, self.state.positions[order.symbol].total_qty,
                                self.prices[order.symbol], t, 'CHANNEL_EXIT')
            else:
                self._open(order, self.prices[order.symbol], t)

    def _funding_order_scenarios(self, simultaneous, pending, t):
        fields = ('state', 'cash', 'margins', 'active_specs', 'accumulated', 'entry_times',
                  'events', 'trades', 'last_specs', 'blocked_symbols')
        original = {key: deepcopy(getattr(self, key)) for key in fields}
        choices = []
        for funding_first in (True, False):
            for key, value in original.items():
                setattr(self, key, deepcopy(value))
            if funding_first:
                for s, row in simultaneous:
                    self._fund(s, row, t)
            self._execute_orders(pending, t)
            if not funding_first:
                for s, row in simultaneous:
                    self._fund(s, row, t)
            choices.append((self._equity(), {key: deepcopy(getattr(self, key)) for key in fields}))
        selected = min(choices, key=lambda x: x[0]) if self.scenario == 'worst_case' else max(choices, key=lambda x: x[0])
        for key, value in selected[1].items():
            setattr(self, key, value)
        self.ambiguities.append(dict(timestamp=str(t), kind='FUNDING_ORDER_TIMESTAMP',
                                     scenario=self.scenario, immediate_equity_range=[min(x[0] for x in choices), max(x[0] for x in choices)]))

    def run(self):
        symbols = sorted(self.daily)
        index = self.minutes[symbols[0]].loc[self.start:self.end].index
        for s in symbols:
            if not self.minutes[s].loc[self.start:self.end].index.equals(index) or not self.marks[s].loc[self.start:self.end].index.equals(index):
                raise ValueError('execution requires aligned complete last/mark minute calendars')
        if index.empty or not index.equals(pd.date_range(index[0], index[-1], freq='min')):
            raise ValueError('missing execution minutes')
        funding_events = {}
        for s, events in self.funding.items():
            for t, row in events.loc[self.start:self.end].iterrows():
                if 'mark_price_at_event' not in row:
                    raise ValueError('funding event requires contemporaneous mark price')
                if t != t.floor('min'):
                    raise ValueError('subminute funding requires tick execution data; minute OHLC cannot order it')
                funding_events.setdefault(t, []).append((s, row))
        timeline = index.union(pd.DatetimeIndex(list(funding_events))) if funding_events else index
        pending = []
        for t in timeline:
            self.blocked_symbols = set()
            is_minute = t in index
            if is_minute:
                bars = {s: self.minutes[s].loc[t] for s in symbols}
                mark_bars = {s: self.marks[s].loc[t] for s in symbols}
                self.prices.update({s: float(bars[s]['open']) for s in symbols})
                # Existing opening gap risk always precedes new exposure.
                for s in list(self.state.positions):
                    self._margin(s, float(mark_bars[s]['open']), t)
                    if s in self.state.positions:
                        p = self.state.positions[s]
                        if p.side * (self.prices[s] - p.stop_price) <= 0:
                            self._close(s, p.total_qty, self.prices[s], t, 'GAP_STOP')
                if t == t.normalize():
                    self._rebalance(t)
                    pending = self._orders(t)
            simultaneous = funding_events.get(t, [])
            if simultaneous and pending:
                orders = {row.get('execution_order') for _, row in simultaneous if row.get('ordering_source')}
                if len(orders) == 1 and len([row for _, row in simultaneous if row.get('ordering_source')]) == len(simultaneous):
                    ordering = next(iter(orders))
                    if ordering not in ('FUNDING_FIRST', 'ORDERS_FIRST'):
                        raise ValueError('invalid historical event ordering')
                    if ordering == 'ORDERS_FIRST':
                        self._execute_orders(pending, t)
                    for s, row in simultaneous:
                        self._fund(s, row, t)
                    if ordering == 'FUNDING_FIRST':
                        self._execute_orders(pending, t)
                else:
                    self._funding_order_scenarios(simultaneous, pending, t)
                pending = []
            else:
                for s, row in simultaneous:
                    self._fund(s, row, t)
            if is_minute:
                self._execute_orders(pending, t)
                pending = []
            if is_minute:
                for s in list(self.state.positions):
                    p = self.state.positions[s]
                    stop_hit = float(bars[s]['low']) <= p.stop_price if p.side == 1 else float(bars[s]['high']) >= p.stop_price
                    adverse_mark = float(mark_bars[s]['low' if p.side == 1 else 'high'])
                    margin_hit = self.machine.ratio(self._isolated(s), adverse_mark, self._spec(s, t)) <= 1
                    if stop_hit and margin_hit:
                        self.ambiguities.append(dict(timestamp=str(t), symbol=s, kind='STOP_LIQUIDATION_ORDER'))
                    if stop_hit and (not margin_hit or self.scenario == 'best_case'):
                        fill = min(float(bars[s]['open']), p.stop_price) if p.side == 1 else max(float(bars[s]['open']), p.stop_price)
                        self._close(s, p.total_qty, fill, t, 'ATR_STOP')
                    else:
                        self._margin(s, adverse_mark, t)
                        if stop_hit and s in self.state.positions:
                            self._close(s, self.state.positions[s].total_qty, p.stop_price, t, 'ATR_STOP')
                self.prices.update({s: float(mark_bars[s]['close']) for s in symbols})
                if t.minute == 59 and t.hour == 23 or t == index[-1]:
                    self.equity.append((t, self._equity()))
        curve = pd.Series([self.initial_equity] + [v for _, v in self.equity],
                          index=[self.start - pd.Timedelta(nanoseconds=1)] + [t for t, _ in self.equity])
        return dict(equity=curve, trades=self.trades, events=self.events, signals=self.signals,
                    ambiguities=self.ambiguities, scenario=self.scenario,
                    open_position_count=len(self.state.positions),
                    liquidation_count=sum(e.get('action') in ('PARTIAL_LIQUIDATION', 'FULL_LIQUIDATION') for e in self.events),
                    execution_model_status='SIMULATED_INTRABAR_AND_LIQUIDATION',
                    limitation='OHLC ordering scenarios are not exhaustive bounds; no queue/ADL reconstruction')
