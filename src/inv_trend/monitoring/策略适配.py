"""Auditable observations from the maintained Turtle engine, without I/O or fills."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from inv_trend.adapters.multi_asset.models.domain import PortfolioState, Position, PositionUnit, SkippedFastTrade
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.adapters.multi_asset.strategy.indicators import compute_turtle_indicators
from inv_trend.core.突破规则 import breakout_direction

from .模型 import digest, frozen, json_value, timeframe


def evidence(value: Any) -> Any:
    """Absent warmup indicators are explicit null, never fabricated zero values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: evidence(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (float, np.floating)) and not math.isfinite(value):
        return None
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, Mapping):
        return {str(key): evidence(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [evidence(item) for item in value]
    return json_value(value)


@dataclass(frozen=True)
class ReferenceAccount:
    mode: str = 'unknown'
    equity: float | None = None
    portfolio: PortfolioState = field(default_factory=PortfolioState)
    position_refs: Mapping[str, str] = field(default_factory=dict)
    observation_id: str = 'unknown'
    observed_at: datetime | None = None

    def __post_init__(self):
        if self.mode not in {'unknown', 'paper', 'observed'}:
            raise ValueError('reference account mode must be unknown/paper/observed')
        if self.equity is not None and (isinstance(self.equity, bool) or not math.isfinite(self.equity) or self.equity <= 0):
            raise ValueError('reference equity must be finite and positive')
        if self.mode == 'paper' and self.equity is None:
            raise ValueError('paper reference requires explicit equity')
        if self.mode == 'observed' and (self.observed_at is None or self.observed_at.utcoffset() is None):
            raise ValueError('observed reference requires an aware timestamp')
        if self.mode != 'unknown' and not self.observation_id:
            raise ValueError('known reference requires observation identity')
        if self.mode == 'unknown' and self.portfolio.positions:
            raise ValueError('unknown accounts cannot assert positions')
        for symbol, position in self.portfolio.positions.items():
            if symbol != position.symbol or position.side not in {-1, 1} or position.system not in {'fast', 'slow'}:
                raise ValueError('invalid reference position identity')
            if not self.position_refs.get(symbol) or not position.units:
                raise ValueError('positions require immutable observation references and units')
            if not math.isfinite(position.stop_price) or position.stop_price <= 0:
                raise ValueError('position stop must be finite and positive')
            for unit in position.units:
                if any(not math.isfinite(value) or value <= 0 for value in (unit.qty, unit.entry_price, unit.n_at_entry)):
                    raise ValueError('invalid observed position unit')
        object.__setattr__(self, 'portfolio', deepcopy(self.portfolio))
        object.__setattr__(self, 'position_refs', frozen(self.position_refs))

    def is_known(self, now: datetime, max_age_seconds: float) -> bool:
        if now.utcoffset() is None:
            raise ValueError('reference evaluation time must be timezone-aware')
        if self.mode == 'paper':
            return True
        if self.mode == 'unknown':
            return False
        age = (now - self.observed_at).total_seconds()
        return 0 <= age <= max_age_seconds

    def to_dict(self):
        return {'mode': self.mode, 'equity': self.equity, 'portfolio': evidence(asdict(self.portfolio)),
                'position_refs': json_value(self.position_refs), 'observation_id': self.observation_id,
                'observed_at': self.observed_at.isoformat() if self.observed_at else None}

    @classmethod
    def from_dict(cls, value):
        value = json_value(value)
        portfolio = value['portfolio']
        positions = {}
        for symbol, record in portfolio['positions'].items():
            units = []
            for item in record['units']:
                item = dict(item)
                if item['entry_time'] is not None:
                    item['entry_time'] = pd.Timestamp(item['entry_time'])
                units.append(PositionUnit(**item))
            positions[symbol] = Position(**(record | {'units': units}))
        state = PortfolioState(positions, portfolio['last_fast_trade_won'],
                               {symbol: SkippedFastTrade(**item)
                                for symbol, item in portfolio['skipped_fast_trades'].items()})
        return cls(**(value | {'portfolio': state, 'observed_at':
            pd.Timestamp(value['observed_at']) if value['observed_at'] else None}))

    @property
    def signature(self):
        return digest(self.to_dict())


@dataclass(frozen=True)
class StrategyObservation:
    direction: int
    setup: bool
    candidate: bool
    close_confirmed: bool
    execution_allowed: bool
    exit_triggered: bool
    exit_code: str
    market: Mapping[str, Any]
    indicators: Mapping[str, Any]
    conditions: Mapping[str, bool]
    intents: tuple[Mapping[str, Any], ...]
    pending: Mapping[str, Any]
    reasons: tuple[str, ...]
    strategy_state: Mapping[str, Any]

    def __post_init__(self):
        for name in ('market', 'indicators', 'conditions', 'pending', 'strategy_state', 'intents'):
            object.__setattr__(self, name, frozen(getattr(self, name)))


class TurtleEvaluator:
    strategy_version = 'multi-asset-turtle-monitor-v1'

    def __init__(self, engine: MultiAssetTurtleStrategy, *, proximity_n: float = .5):
        if engine.rules.trigger_mode != 'close':
            raise ValueError('confirmed monitoring requires the shared close-confirmation strategy')
        if not math.isfinite(proximity_n) or proximity_n < 0:
            raise ValueError('invalid setup proximity')
        self.engine = engine
        self.proximity_n = proximity_n

    def evaluate(self, bars: pd.DataFrame, symbol: str, frame: str, account: ReferenceAccount,
                 *, account_known: bool, pending: Mapping[str, Any] | None = None) -> StrategyObservation:
        timeframe(frame)
        if bars.empty or symbol not in self.engine.specs:
            raise ValueError('strategy needs nonempty bars and a registered asset specification')
        prepared = compute_turtle_indicators(bars, self.engine.rules)
        row = prepared.iloc[-1]
        spec, rules = self.engine.specs[symbol], self.engine.rules
        n = evidence(row.get('n'))
        close = float(row['close'])
        complete = isinstance(row.get('is_complete'), (bool, np.bool_)) and bool(row['is_complete'])
        warm = n is not None and n > 0 and math.isfinite(close) and close > 0
        enabled = [(name, period) for name, period, active in (
            ('fast', rules.fast_entry, rules.fast_system_enabled),
            ('slow', rules.slow_entry, rules.slow_system_enabled)) if active]
        close_hits, raw_hits = [], []
        setup, intraday_touch = False, False
        if warm:
            for system, period in enabled:
                side = self.engine._breakout_signal(row, period, n)
                if side and complete:
                    close_hits.append((system, period, side))
                single = breakout_direction(row, period, n, buffer_n=rules.breakout_buffer_n, trigger_mode='close')
                touched = breakout_direction(row, period, n, buffer_n=rules.breakout_buffer_n, trigger_mode='intraday')
                intraday_touch = intraday_touch or touched is not None
                if single or touched:
                    raw_hits.append((system, period, single or touched))
                for name in ('high', 'low'):
                    level = evidence(row.get(f'{name}_{period}'))
                    if level is not None and abs(close - level) / n <= self.proximity_n:
                        setup = True
        working_state = deepcopy(account.portfolio)
        # All order calculations use the same engine as batch backtests. Missing
        # other-asset marks prevent entry eligibility rather than using entry
        # prices as if they were current portfolio marks.
        marks_known = not (set(working_state.positions) - {symbol})
        budget_known = account_known and account.equity is not None and marks_known
        orders = self.engine.generate_orders({symbol: row}, working_state, account.equity,
                                              tradable_symbols={symbol}) if budget_known and warm and complete else []
        opening = next((order for order in orders if order.action == 'open'), None)
        fast_history_known = not (account.mode == 'observed' and opening and opening.system == 'fast'
                                  and rules.skip_fast_after_win
                                  and type(account.portfolio.last_fast_trade_won.get(symbol)) is not bool)
        entry_allowed = opening is not None and fast_history_known
        selected = next((hit for hit in close_hits if opening and hit[0] == opening.system and hit[2] == opening.side), None)
        selected = selected or next(iter(close_hits or raw_hits), None)
        direction = opening.side if opening else selected[2] if selected else 0
        pending = dict(pending or {})
        holding_candidate = False
        if pending and warm and rules.entry_confirmation_bars > 1:
            start = pd.Timestamp(pending['started_at'])
            elapsed_bars = int((prepared.index > start).sum())
            side, level = pending['direction'], pending['anchor']
            holding_candidate = elapsed_bars < rules.entry_confirmation_bars and (
                close > level if side == 1 else close < level)
            if holding_candidate and not direction:
                direction = side
        if direction and selected and not pending:
            _, period, side = selected
            pending = {'started_at': row.name.isoformat(), 'direction': side,
                       'anchor': float(row[f'{"high" if side == 1 else "low"}_{period}']),
                       'system': selected[0], 'period': period}
        gates = self.engine.entry_filter_conditions(row, direction or 1, close) if warm else {
            'trend_gate': False, 'adx_gate': False, 'atr_percentile_gate': False}
        conditions = {**gates, 'ma_gate': gates['trend_gate'], 'trend_gate': all(gates.values()),
                      'indicator_ready': bool(warm), 'intraday_touch': bool(intraday_touch),
                      'bar_complete': bool(complete),
                      'close_confirmation': bool(close_hits), 'account_gate': bool(account_known),
                      'portfolio_marks_gate': bool(marks_known), 'budget_gate': bool(budget_known),
                      'execution_gate': bool(entry_allowed), 'fast_history_gate': bool(fast_history_known),
                      'direction_allowed': bool(direction and (spec.can_long if direction == 1 else rules.allow_short and spec.can_short))}
        position = account.portfolio.positions.get(symbol) if account_known else None
        exit_order = self.engine._exit_order(symbol, row, position, spec, account.equity or 0.) if position and complete else None
        exit_code = 'STOP_EXIT' if exit_order and 'stop' in exit_order.reason else 'TREND_EXIT'
        market = {name: evidence(row.get(name)) for name in ('open', 'high', 'low', 'close', 'volume')}
        indicators = {str(name): evidence(value) for name, value in row.items()
                      if name in {'n', 'atr', 'adx', 'entry_atr_percentile'} or str(name).startswith(('high_', 'low_', 'sma_', 'entry_'))}
        reasons = []
        if not warm:
            reasons.append('INDICATOR_WARMUP')
        if not account_known:
            reasons.append('REFERENCE_ACCOUNT_UNKNOWN_OR_STALE')
        if not marks_known:
            reasons.append('OTHER_POSITION_MARKS_UNVERIFIED')
        if raw_hits and not close_hits:
            reasons.append('AWAITING_CLOSE_CONFIRMATION')
        if close_hits and not entry_allowed:
            reasons.append('ENTRY_NOT_ADMITTED_BY_SHARED_ENGINE')
        return StrategyObservation(
            direction, bool(setup), bool(raw_hits or holding_candidate or close_hits), bool(close_hits),
            bool(entry_allowed), exit_order is not None, exit_code, market, indicators, conditions,
            tuple(evidence(asdict(order)) for order in orders), pending if holding_candidate or selected else {},
            tuple(reasons), evidence(asdict(working_state)),
        )
