"""Replay the existing eligibility checker using causal account and market inputs."""
from __future__ import annotations

import pandas as pd

from inv_trend.adapters.detector.eligibility.checker import AccountSnapshot, TradeEligibilityChecker
from inv_trend.adapters.detector.models import AssetConfig, Market
from inv_trend.core.永续风控 import specification_at, stop_loss_value


class HistoricalEligibility:
    def __init__(self, specs, contexts=()):
        self.specs, self.contexts = specs, tuple(contexts)
        self.checker = TradeEligibilityChecker()

    def __call__(self, symbol, date, state, equity, *, order, bars, cash, equity_history):
        # Execution-readiness and prior strategy certification are historical
        # facts. They cannot be manufactured from the strategy being tested.
        available = [c for c in self.contexts if c.get('instrument') == symbol and
                     pd.Timestamp(c['available_at']) <= date + pd.Timedelta(days=1) and
                     pd.Timestamp(c['effective_from']) <= date and
                     (not c.get('effective_to') or date < pd.Timestamp(c['effective_to']))]
        if len(available) != 1:
            return dict(status='NOT_EVALUATED', evaluated=False, passed=False,
                        reason='MISSING_OR_OVERLAPPING_HISTORICAL_EXECUTION_CONTEXT')
        context = available[0]
        if not context.get('source') or not all(type(context.get(k)) is bool for k in ('backtest_validated', 'execution_ready')):
            return dict(status='NOT_EVALUATED', evaluated=False, passed=False, reason='INVALID_CONTEXT_EVIDENCE')
        spec = specification_at(self.specs, symbol, date + pd.Timedelta(days=1))
        snapshot = bars.loc[:date].copy()
        if snapshot.empty:
            return dict(status='NOT_EVALUATED', evaluated=False, passed=False, reason='MISSING_HISTORY')
        snapshot['atr'] = snapshot['n']
        snapshot['atr_pct'] = snapshot['atr'] / snapshot['close']
        snapshot['is_closed'] = True
        row = snapshot.iloc[-1]
        period = int(order.metadata.get('entry_period', 20 if order.system == 'fast' else 55))
        channel = float(row[f'high_{period}' if order.side == 1 else f'low_{period}'])
        risks = {}
        for s, p in state.positions.items():
            contract = specification_at(self.specs, s, date + pd.Timedelta(days=1))
            risks[s] = stop_loss_value(p.side, p.total_qty, p.avg_entry_price, p.stop_price,
                                      contract.contract_value, 2 * contract.fee_rate) / equity
        peak = max([equity] + [float(v) for _, v in equity_history])
        account = AccountSnapshot(equity=equity, cash=cash, open_positions=risks,
                                  asset_class_exposure={'crypto': sum(risks.values())},
                                  risk_group_exposure={'crypto': sum(risks.values())},
                                  current_drawdown=max(0., 1 - equity / peak))
        asset = AssetConfig(symbol, symbol, Market.CRYPTO, 'OKX', ('D1',),
                            price_type='perpetual', currency='USDT', point_value=spec.contract_value,
                            risk_unit_pct=.005, max_symbol_risk_pct=.01)
        signal = dict(symbol=symbol, instrument=symbol, market='crypto', timeframe='D1',
                      signal_type='SYSTEM1_BREAKOUT' if order.system == 'fast' else 'SYSTEM2_BREAKOUT',
                      direction='long' if order.side == 1 else 'short', signal_time=str(date),
                      trigger_price=float(row['close']), atr=float(row['n']), atr_pct=float(row['atr_pct']),
                      stop_price=order.stop_price, distance_to_breakout_atr=abs(float(row['close']) - channel) / float(row['n']),
                      channel_high=float(row[f'high_{period}']), channel_low=float(row[f'low_{period}']),
                      data_source='OKX', suggested_risk_unit=.005,
                      metadata={'historical_context_source': context['source']})
        result = self.checker.evaluate(signal, snapshot, asset, account,
                                       backtest_validated=context['backtest_validated'],
                                       execution_ready=context['execution_ready'], require_account=True)
        return dict(status='PASSED' if result.trade_eligible else 'BLOCKED', evaluated=True,
                    passed=result.trade_eligible, evidence=result.to_dict(), source=context['source'])
