"""批量投影必须与维护中的策略引擎及其数据/参数身份一致。"""
from dataclasses import replace

import pandas as pd
import pytest

from inv_trend.adapters.multi_asset.models.domain import AssetSpec, PortfolioState
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.application.backtest.projector import StrategyReplayProjector
from tests.test_海龟入场过滤合同 import bars
from tests.unit.test_strategy_backtest_stage import _bars, _source


@pytest.mark.parametrize('key, first, second, enabled', [
    ('entry_adx_min', 0., 20., {}),
    ('entry_adx_period', 14, 7, {'rules.entry_adx_min': 20.}),
    ('entry_confirmation_bars', 1, 3, {}),
    ('entry_atr_percentile_max', 1., .9, {}),
    ('entry_atr_percentile_lookback', 120, 30, {'rules.entry_atr_percentile_max': .9}),
])
def test_signal_filter_changes_never_share_prepared_cache(key, first, second, enabled):
    projector = StrategyReplayProjector(_source())
    data = {'TEST': _bars()}
    a, a_store, _ = projector.project(data, enabled | {'rules.' + key: first})
    b, b_store, _ = projector.project(data, enabled | {'rules.' + key: second})
    assert a.signal_parameter_hash != b.signal_parameter_hash
    assert a_store is not b_store
    assert projector.cache_size == 2


@pytest.mark.parametrize('side, prices', [(1, [12., 11., 11.3]), (-1, [8., 9., 8.7])])
def test_projected_orders_preserve_multi_close_confirmation(side, prices):
    projector = StrategyReplayProjector(_source())
    data = {'TEST': bars([10.] * 5 + prices)}
    bundle, store, rules = projector.project(data, {
        'rules.n_period': 2, 'rules.fast_entry': 3, 'rules.slow_entry': 5,
        'rules.fast_system_enabled': False, 'rules.entry_confirmation_bars': 3,
        'rules.entry_ma_period': 0,
    })
    specs = {'TEST': AssetSpec('TEST', 'metal', 'm')}
    row = store.by_symbol['TEST'].bars.iloc[-1]
    direct = MultiAssetTurtleStrategy(specs, rules).generate_orders(
        {'TEST': row}, PortfolioState(), 100000.,
    )
    assert len(direct) == 1 and direct[0].side == side
    projected = projector.strategy(bundle, specs, rules).generate_orders_for_date(
        row.name, {'TEST': row}, PortfolioState(), 100000.,
    )
    assert projected == direct
    event = next(event for event in bundle.events
                 if event['signal_time'] == row.name.isoformat() and event['system'] == 'slow')
    assert event['channel_value'] == direct[0].metadata['breakout_level']


def test_changed_data_cannot_reuse_a_different_input_frame():
    projector = StrategyReplayProjector(_source())
    first = _bars()
    a, a_store, _ = projector.project({'TEST': first}, {})
    changed = first.copy()
    changed.iloc[-1, changed.columns.get_loc('close')] += .2
    b, b_store, _ = projector.project({'TEST': changed}, {})
    assert b_store is not a_store
    assert b.prepared_data_hashes != a.prepared_data_hashes
    assert b_store.by_symbol['TEST'].bars.iloc[-1]['close'] == changed.iloc[-1]['close']


def test_projection_preserves_h4_source_identity():
    source = _source()
    source = replace(source, instruments=(replace(source.instruments[0], timeframe='H4'),))
    frame = bars([10.] * 5 + [12., 11., 11.3])
    frame.index = pd.date_range('2024-01-01', periods=len(frame), freq='4h', tz='UTC')
    bundle, _, _ = StrategyReplayProjector(source).project({'TEST': frame}, {
        'rules.n_period': 2, 'rules.fast_entry': 3, 'rules.slow_entry': 5,
    })
    assert bundle.events
    assert {event['timeframe'] for event in bundle.events} == {'H4'}
