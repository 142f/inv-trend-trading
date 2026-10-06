"""Canonical filtering and explicit execution quantity limits must survive resolution."""
from copy import deepcopy

import pytest

from inv_trend.config import load_strategy_mapping
from inv_trend.application.backtest.projector import resolve_rules
from inv_trend.adapters.multi_asset.models.domain import PortfolioState, TurtleRules
from inv_trend.adapters.multi_asset.profiles.asset_profiles import build_asset_specs
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.adapters.multi_asset.strategy.indicators import compute_turtle_indicators
from inv_trend.adapters.multi_asset.config.settings import load_config
import yaml
from tests.test_海龟入场过滤合同 import bars


@pytest.mark.parametrize('name,value', [('entry_adx_min', 25.), ('entry_adx_period', 10),
    ('entry_confirmation_bars', 3), ('entry_atr_percentile_max', .7),
    ('entry_atr_percentile_lookback', 50), ('risk_atr_fraction_target', .02)])
def test_shared_canonical_configuration_accepts_valid_existing_strategy_filters(name, value):
    config = deepcopy(load_strategy_mapping())
    config['turtle']['rules'][name] = value
    assert getattr(resolve_rules(config, {}), name) == value


def test_fractional_default_does_not_overwrite_explicit_minimum_quantity():
    spec = build_asset_specs(['BTC'], {'BTC': {'min_qty': .01}})['BTC']
    assert spec.qty_step == .001
    assert spec.min_qty == .01


def test_explicit_minimum_quantity_really_blocks_shared_engine_order():
    spec = build_asset_specs(['BTC'], {'BTC': {'min_qty': 100000.}})['BTC']
    rules = TurtleRules(n_period=2, fast_entry=3, slow_entry=5, fast_exit=2, slow_exit=3,
                        skip_fast_after_win=False, fast_system_enabled=False)
    prepared = compute_turtle_indicators(bars([10.] * 8 + [12.]), rules)
    orders = MultiAssetTurtleStrategy({'BTC': spec}, rules).generate_orders(
        {'BTC': prepared.iloc[-1].to_dict()}, PortfolioState(), 100000.)
    assert orders == []


def test_backtest_loader_and_monitor_resolve_the_same_nondefault_canonical_rules(tmp_path):
    config = deepcopy(load_strategy_mapping())
    config['turtle']['rules'].update(entry_adx_min=25., entry_confirmation_bars=3,
        entry_ma_period=60, allow_short=False, skip_fast_after_win=False, max_total_leverage=.75)
    path = tmp_path / 'shared.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    assert TurtleRules(**load_config(path).rules) == resolve_rules(config, {})
