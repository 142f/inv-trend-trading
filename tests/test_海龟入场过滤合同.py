"""预注册过滤规则必须生效，并只使用当时已完成的行情。"""
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from inv_trend.adapters.multi_asset.models.domain import AssetSpec, PortfolioState, TurtleRules
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.adapters.multi_asset.strategy.indicators import compute_turtle_indicators, _with_indicators


def strategy(rules):
    return MultiAssetTurtleStrategy({'TEST': AssetSpec('TEST', 'metal', 'm')}, rules)


def row(side):
    return {'open': 10., 'high': 14., 'low': 6., 'close': 12. if side == 1 else 8.,
            'n': 1., 'high_20': 11., 'low_20': 9., 'high_55': 11., 'low_55': 9.,
            'adx': 20., 'entry_atr_percentile': .5}


def test_registered_candidates_are_supported_without_dropping_rules():
    protocol = json.loads((Path(__file__).parents[1] / 'config/滚动验证协议_v2.json').read_text(encoding='utf-8'))
    for candidate in protocol['candidates']:
        rules = TurtleRules(**candidate['rules'])
        for key, value in candidate['rules'].items():
            assert getattr(rules, key) == value


@pytest.mark.parametrize('side', [1, -1])
@pytest.mark.parametrize('adx, allowed', [(19.9, False), (20., True), (30., True), (None, False), (np.nan, False)])
def test_adx_threshold_blocks_weak_or_unknown_trend(side, adx, allowed):
    engine = strategy(TurtleRules(entry_adx_min=20.))
    values = row(side) | {'adx': adx}
    orders = engine.generate_orders({'TEST': values}, PortfolioState(), 100000.)
    assert bool(orders) == allowed
    if orders:
        assert orders[0].side == side


@pytest.mark.parametrize('side', [1, -1])
@pytest.mark.parametrize('percentile, allowed', [(.89, True), (.9, True), (.91, False), (None, False), (np.nan, False)])
def test_atr_percentile_threshold_is_inclusive_and_fail_closed(side, percentile, allowed):
    engine = strategy(TurtleRules(entry_atr_percentile_max=.9))
    values = row(side) | {'entry_atr_percentile': percentile}
    assert bool(engine.generate_orders({'TEST': values}, PortfolioState(), 100000.)) == allowed


def bars(prices):
    prices = np.asarray(prices, dtype=float)
    return pd.DataFrame({'open': prices, 'high': prices + .2, 'low': prices - .2,
                         'close': prices, 'volume': 10000.},
                        index=pd.date_range('2024-01-01', periods=len(prices), tz='UTC'))


@pytest.mark.parametrize('side, prices', [(1, [12., 11., 11.3]), (-1, [8., 9., 8.7])])
def test_three_close_confirmation_uses_first_breakout_channel(side, prices):
    rules = TurtleRules(n_period=2, fast_entry=3, slow_entry=5,
                        fast_system_enabled=False, entry_confirmation_bars=3)
    frame = compute_turtle_indicators(bars([10.] * 5 + prices), rules)
    engine = strategy(rules)
    for index in (5, 6):
        assert not engine.generate_orders({'TEST': frame.iloc[index]}, PortfolioState(), 100000.)
    orders = engine.generate_orders({'TEST': frame.iloc[7]}, PortfolioState(), 100000.)
    assert len(orders) == 1 and orders[0].side == side
    # The last close need not beat the high/low established by the first
    # breakout bar. It must hold beyond the original, pre-breakout channel.
    assert orders[0].metadata['breakout_level'] == pytest.approx(10.2 if side == 1 else 9.8)


@pytest.mark.parametrize('side, prices', [(1, [12., 11., 10.2]), (-1, [8., 9., 9.8])])
def test_equal_channel_close_invalidates_multi_bar_confirmation(side, prices):
    rules = TurtleRules(n_period=2, fast_entry=3, slow_entry=5,
                        fast_system_enabled=False, entry_confirmation_bars=3)
    frame = compute_turtle_indicators(bars([10.] * 5 + prices), rules)
    assert not strategy(rules).generate_orders({'TEST': frame.iloc[-1]}, PortfolioState(), 100000.)


@pytest.mark.parametrize('incomplete_bar', [5, 6, 7])
def test_incomplete_bar_cannot_count_as_close_confirmation(incomplete_bar):
    rules = TurtleRules(n_period=2, fast_entry=3, slow_entry=5,
                        fast_system_enabled=False, entry_confirmation_bars=3)
    source = bars([10.] * 5 + [12., 11., 11.3])
    source['is_complete'] = True
    source.loc[source.index[incomplete_bar], 'is_complete'] = False
    frame = compute_turtle_indicators(source, rules)
    assert not strategy(rules).generate_orders({'TEST': frame.iloc[-1]}, PortfolioState(), 100000.)


def test_missing_confirmation_evidence_cannot_fall_back_to_single_breakout():
    assert not strategy(TurtleRules(entry_confirmation_bars=3)).generate_orders(
        {'TEST': row(1)}, PortfolioState(), 100000.)


def test_disabled_filters_preserve_entry_without_optional_evidence():
    values = row(1)
    values.pop('adx')
    values.pop('entry_atr_percentile')
    assert strategy(TurtleRules()).generate_orders({'TEST': values}, PortfolioState(), 100000.)


@pytest.mark.parametrize('n, expected_quantity', [(.1, 5000.), (1., 120.)])
def test_volatility_risk_target_only_reduces_positions(n, expected_quantity):
    values = row(1) | {'n': n}
    normal = strategy(TurtleRules()).generate_orders({'TEST': values}, PortfolioState(), 100000.)
    reduced = strategy(TurtleRules(risk_atr_fraction_target=.02)).generate_orders(
        {'TEST': values}, PortfolioState(), 100000.)
    assert reduced[0].qty == expected_quantity
    assert reduced[0].qty <= normal[0].qty
    assert reduced[0].risk_1n_pct <= normal[0].risk_1n_pct
    assert 0.0 < reduced[0].metadata['risk_scale'] <= 1.0


def test_filters_and_confirmation_are_prefix_causal_and_cache_is_rule_bound():
    prices = 100. + np.arange(220) * .1 + np.sin(np.arange(220))
    source = bars(prices)
    rules = TurtleRules(entry_adx_min=20., entry_atr_percentile_max=.9,
                        entry_confirmation_bars=3)
    prefix = compute_turtle_indicators(source.iloc[:180], rules)
    changed = source.copy()
    changed.iloc[180:, :4] *= 1000.
    full = compute_turtle_indicators(changed, rules)
    pd.testing.assert_frame_equal(prefix, full.iloc[:180])
    alternative = replace(rules, entry_confirmation_bars=2, entry_adx_period=10,
                          entry_atr_percentile_lookback=80)
    pd.testing.assert_frame_equal(_with_indicators(prefix, alternative),
                                  compute_turtle_indicators(source.iloc[:180], alternative))


@pytest.mark.parametrize('invalid', [
    {'entry_adx_min': -1.}, {'entry_adx_min': np.nan}, {'entry_adx_min': 101.},
    {'entry_adx_period': 1}, {'entry_adx_period': True},
    {'entry_atr_percentile_max': -0.1}, {'entry_atr_percentile_max': 1.1},
    {'entry_atr_percentile_max': np.inf}, {'entry_atr_percentile_lookback': 1},
    {'entry_confirmation_bars': 0}, {'entry_confirmation_bars': 1.5},
    {'entry_confirmation_bars': True},
    {'entry_confirmation_bars': 3, 'trigger_mode': 'intraday'},
    {'risk_atr_fraction_target': -0.1}, {'risk_atr_fraction_target': np.nan},
])
def test_invalid_entry_filter_contract_is_rejected(invalid):
    with pytest.raises(ValueError):
        TurtleRules(**invalid)
