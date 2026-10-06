"""Historical replay loads one verified Repository version and uses MonitorScanner."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy

from .模型 import digest, timeframe
from .扫描器 import MonitorScanner, _indexed
from .策略适配 import ReferenceAccount, evidence


@dataclass(frozen=True)
class ReplayResult:
    symbol: str
    timeframe: str
    dataset_version: str
    decisions: tuple[dict, ...]
    comparison: dict | None = None

    def to_dict(self):
        return {'symbol': self.symbol, 'timeframe': self.timeframe,
                'dataset_version': self.dataset_version, 'decisions': list(self.decisions),
                'comparison': self.comparison}


class HistoricalReplay:
    def __init__(self, scanner: MonitorScanner):
        self.scanner = scanner

    def run(self, symbol: str, frame: str, dataset_version: str, *,
            account: ReferenceAccount | None = None,
            account_at_bar: Callable[[pd.Timestamp], ReferenceAccount] | None = None,
            compare_backtest: bool = False, reference_equity: float = 100000.) -> ReplayResult:
        if self.scanner.store.purpose != 'replay':
            raise ValueError('historical replay requires a separate replay event store')
        symbol, frame = symbol.upper(), timeframe(frame)
        # No current-pointer read, CSV fallback or direct Parquet read occurs.
        bars = self.scanner.repository.load_bars_version(symbol, frame, dataset_version, adjusted=False)
        bars = _indexed(bars)
        instrument = self.scanner.repository.instrument(symbol)
        recorder = None
        if compare_backtest:
            if account is not None or account_at_bar is not None:
                raise ValueError('choose backtest reference or explicit reference inputs, not both')
            recorder = _BacktestReference(self.scanner, symbol, reference_equity)
            recorder.run(bars)
            account_at_bar = recorder.account_at_bar
        decisions = []
        for index, bar_time in enumerate(bars.index):
            prefix = bars.iloc[:index + 1].copy()
            prefix.attrs = deepcopy(bars.attrs)
            end = pd.Timestamp(prefix.iloc[-1]['bar_end']) if 'bar_end' in prefix else bar_time + pd.Timedelta(
                '1D' if frame == 'D1' else '4h')
            evaluated_at = (end + pd.Timedelta(seconds=self.scanner.freshness.grace_seconds + 1)).to_pydatetime()
            selected = account_at_bar(bar_time) if account_at_bar else account or ReferenceAccount()
            result = self.scanner._scan_verified_prefix(prefix, instrument, frame, now=evaluated_at,
                                                         account=selected, mode='replay')
            decisions.append(result['snapshot'])
        comparison = recorder.compare(decisions) if recorder else None
        return ReplayResult(symbol, frame, dataset_version, tuple(decisions), comparison)


class _BacktestReference:
    """Capture simulated close-decision inputs; never connect to a broker.

    Comparison covers strategy intents for identical bar/portfolio/equity
    inputs. It does not pretend that monitor alerts are fills or PnL.
    """
    def __init__(self, scanner, symbol, equity):
        self.scanner, self.symbol, self.equity = scanner, symbol, equity
        self.engine = MultiAssetTurtleStrategy(scanner.evaluator.engine.specs, scanner.evaluator.engine.rules)
        self.references = {}
        self.orders = {}

    def generate_orders_for_date(self, when, rows, state, equity, tradable_symbols=None):
        row = rows.get(self.symbol)
        if row is None:
            return []
        bar_time = pd.Timestamp(row.get('timestamp', when))
        refs = {symbol: 'paper:' + digest({'symbol': symbol, 'side': position.side,
                                        'system': position.system,
                                        'first_entry_time': evidence(position.first_entry_time)})
                for symbol, position in state.positions.items()}
        self.references[bar_time] = ReferenceAccount('paper', equity, state, refs,
                                                     observation_id='backtest-close:' + bar_time.isoformat())
        orders = self.engine.generate_orders(rows, state, equity, tradable_symbols=tradable_symbols)
        from dataclasses import asdict
        self.orders[bar_time] = [evidence(asdict(order)) for order in orders]
        return orders

    def run(self, bars):
        self.result = TurtleBacktester({self.symbol: bars}, self.engine.specs, self.engine.rules,
                                      initial_equity=self.equity, liquidate_at_end=False,
                                      strategy=self).run()

    def account_at_bar(self, bar_time):
        # The maintained backtester has no entry evaluation at its terminal
        # horizon. Mark that last input unknown instead of fabricating a fill.
        return self.references.get(bar_time, ReferenceAccount())

    def compare(self, decisions):
        mismatches = []
        compared = 0
        for snapshot in decisions:
            when = pd.Timestamp(snapshot['bar_time'])
            if when not in self.orders:
                continue
            compared += 1
            actual = snapshot.get('state_context', {}).get('intents', [])
            if digest(actual) != digest(self.orders[when]):
                mismatches.append({'bar_time': when.isoformat(), 'monitor': actual,
                                   'backtest': self.orders[when]})
        return {'scope': 'same bar and captured simulated portfolio/equity strategy intents',
                'compared_bars': compared, 'mismatches': mismatches,
                'uncompared_bars': len(decisions) - compared,
                'execution_or_pnl_comparison': False}
