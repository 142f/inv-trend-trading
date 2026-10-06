"""CLI uses shared config/Repository and keeps scans, replay and watchdog isolated."""
from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest
import yaml

from inv_trend.cli.监控运行 import build_scanner, main
from inv_trend.config import load_strategy_mapping
from inv_trend.application.backtest.projector import resolve_rules
from inv_trend.data.provider_factory import create_default_providers
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.运行配置 import RuntimeConfig
from tests.test_监控扫描与回放合同 import market as market
from tests.test_数据封版合同 import service as service


def config_for(market, tmp_path):
    repository, _ = market
    return RuntimeConfig(data_root=repository.lake.root, runtime_dir=tmp_path / 'runtime',
                         symbols=('BTC',), timeframes=('D1',), refresh=False)


def small_strategy(tmp_path):
    canonical = deepcopy(load_strategy_mapping())
    canonical['turtle']['rules'].update(n_period=2, fast_entry=3, slow_entry=5, fast_exit=2, slow_exit=3,
        skip_fast_after_win=False, allow_short=False, entry_ma_period=9, max_total_leverage=.75)
    path = tmp_path / 'shared.yaml'
    path.write_text(yaml.safe_dump(canonical), encoding='utf-8')
    return path, canonical


def test_monitor_bootstrap_honors_all_canonical_rules_and_backtest_uses_same_resolution(market, tmp_path):
    path, canonical = small_strategy(tmp_path)
    scanner = build_scanner(replace(config_for(market, tmp_path), strategy_config=path))
    assert scanner.evaluator.engine.rules == resolve_rules(canonical, {})
    assert scanner.evaluator.engine.rules.allow_short is False
    assert scanner.evaluator.engine.rules.skip_fast_after_win is False
    assert scanner.evaluator.engine.rules.entry_ma_period == 9
    assert scanner.evaluator.engine.rules.max_total_leverage == .75
    assert resolve_rules(canonical, {'rules.entry_ma_period': 5}).entry_ma_period == 5
    assert scanner.evaluator.engine.specs['BTC'].qty_step == .001


def test_registered_equities_and_metals_do_not_become_generic_other_assets(market, tmp_path):
    config = replace(config_for(market, tmp_path), symbols=('BTC', 'ETH', 'XAU', 'XAG', 'QQQ', 'SPY', 'MSFT'),
                     timeframes=('D1', 'H4'))
    specs = build_scanner(config).evaluator.engine.specs
    assert specs['MSFT'].asset_class == 'equity' and specs['MSFT'].cluster == 'us_equity'
    assert specs['XAU'].asset_class == 'precious_metal'
    assert specs['ETH'].qty_step == .001


def test_scan_command_never_constructs_network_providers_or_uses_unknown_execution_account(market, tmp_path, monkeypatch, capsys):
    repository, _ = market
    module = sys.modules[build_scanner.__module__]
    def forbidden(*args, **kwargs):
        pytest.fail('read-only scan must not construct network providers')
    monkeypatch.setattr(module, 'create_default_providers', forbidden)
    assert main(['--data-root', str(repository.lake.root), '--runtime-dir', str(tmp_path / 'runtime'),
                 'scan', '--symbols', 'BTC', '--timeframes', '1D']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['targets'] == 1
    store = EventStore(tmp_path / 'runtime' / '监控事件.sqlite3')
    asset_decisions = [row['payload'] for row in store.rows('decisions') if row['symbol'] == 'BTC']
    snapshot = json.loads(asset_decisions[0]) if isinstance(asset_decisions[0], str) else asset_decisions[0]
    assert snapshot['conditions']['execution_gate'] is False
    assert snapshot['conditions']['freshness_gate'] is False
    assert snapshot['state_after'] != 'CONFIRMED_ENTRY'
    assert store.rows('outbox')


def test_missing_period_scan_reports_failure_and_saves_no_market_signal(market, tmp_path, capsys):
    repository, _ = market
    assert main(['--data-root', str(repository.lake.root), '--runtime-dir', str(tmp_path / 'runtime'),
                 'scan', '--symbols', 'BTC', '--timeframes', 'H4']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['results'][0]['status'] == 'FAILED'
    store = EventStore(tmp_path / 'runtime' / '监控事件.sqlite3')
    assert all(row['kind'] != 'MARKET_SIGNAL' for row in store.rows('events'))


def test_watchdog_runs_even_if_market_repository_cannot_start_and_does_not_fake_heartbeat(tmp_path, capsys):
    directory = tmp_path / 'runtime'
    result = main(['--data-root', str(tmp_path / 'nonexistent-data'), '--runtime-dir', str(directory), 'watchdog'])
    assert result == 2
    assert json.loads(capsys.readouterr().out)['checks'] == ['HEARTBEAT_MISSING']
    store = EventStore(directory / '监控事件.sqlite3')
    from inv_trend.monitoring.执行记录 import RuntimeLedger
    assert RuntimeLedger(store, tmp_path / 'nonexistent-data').heartbeats() == []


def test_cli_replay_is_fixed_version_and_never_enters_live_outbox(market, tmp_path, capsys):
    repository, version = market
    path, _ = small_strategy(tmp_path)
    config = tmp_path / 'runtime.yaml'
    config.write_text(yaml.safe_dump({'data_root': str(repository.lake.root), 'runtime_dir': str(tmp_path / 'runtime'),
                                    'strategy_config': str(path), 'refresh': False}), encoding='utf-8')
    assert main(['--config', str(config), 'replay', '--symbol', 'BTC', '--timeframe', 'D1',
                 '--dataset-version', version, '--compare-backtest', '--paper-equity', '100000']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload['decisions']) == 15 and payload['comparison']['mismatches'] == []
    store = EventStore(tmp_path / 'runtime' / '历史回放.sqlite3', purpose='replay')
    assert store.rows('outbox') == []
    assert not (tmp_path / 'runtime' / '监控事件.sqlite3').exists()


def test_refresh_bootstrap_does_not_read_production_credential_environment(monkeypatch):
    import inv_trend.data.provider_factory as factory
    original = factory.os.getenv
    def reject_secret(key, *args):
        if key == 'ALPHAVANTAGE_API_KEY':
            pytest.fail('monitoring must not read production provider credentials by default')
        return original(key, *args)
    monkeypatch.setattr(factory.os, 'getenv', reject_secret)
    assert set(create_default_providers(credential_providers=False)) == {'binance', 'dukascopy', 'yahoo_chart'}


def test_execution_contract_cannot_replace_registered_price_identity(market, tmp_path):
    path, canonical = small_strategy(tmp_path)
    canonical['risk']['contracts']['BTC'] = {'asset_class': 'equity'}
    path.write_text(yaml.safe_dump(canonical), encoding='utf-8')
    with pytest.raises(ValueError, match='data identity'):
        build_scanner(replace(config_for(market, tmp_path), strategy_config=path))
