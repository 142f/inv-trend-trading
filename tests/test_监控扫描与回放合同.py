"""真实Repository、共享策略、监控状态和逐根回放的集成合同。"""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pandas as pd
import pytest

from inv_trend.adapters.multi_asset.models.domain import AssetSpec, PortfolioState, Position, PositionUnit, TurtleRules
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.data.api import HistoricalDataService
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.扫描器 import CodeVersion, MonitorScanner
from inv_trend.monitoring.回放 import HistoricalReplay
from inv_trend.monitoring.策略适配 import ReferenceAccount, TurtleEvaluator
from inv_trend.monitoring.模型 import Phase
from tests.test_historical_data import FakeProvider
from tests.test_数据封版合同 import service as service
from tests.test_海龟入场过滤合同 import bars as price_bars


@pytest.fixture
def market(service, tmp_path):
    return market_repository(service, tmp_path / 'monitor-market')


def market_repository(service, root, *, timeframe='D1', future_prices=None):
    import json
    root.mkdir()
    if hasattr(service.lake, 'store'):
        from inv_trend.storage.结构化存储_v3 import UnifiedStore
        UnifiedStore(root, create=True)
    else:
        engine = service.lake._catalog_engine
        (root / 'catalog_backend.json').write_text(json.dumps({
            'backend': engine, 'catalog_path': 'catalog.' + ('duckdb' if engine == 'duckdb' else 'sqlite3'),
            'schema_version': 1}), encoding='utf-8')
    frame = price_bars([10.] * 6 + [12., 11., 11.3, 11.5, 11.7, 11.6] +
                       (future_prices or [9.5, 9.4, 9.3]))
    frequency = '1D' if timeframe == 'D1' else '4h'
    frame.index = pd.date_range('2024-01-01', periods=len(frame), freq=frequency, tz='UTC')
    frame['timestamp'] = frame.index
    frame['is_complete'] = True
    provider = FakeProvider(frame)
    repository = HistoricalDataService(root, providers={'binance': provider})
    manifest = repository.ingest('BTC', timeframe, frame.index[0].to_pydatetime(),
                                 (frame.index[-1] + pd.Timedelta(frequency)).to_pydatetime())
    assert manifest.quality_passed is True
    return repository, manifest.dataset_version


def scanner(repository, path, *, purpose='live'):
    rules = TurtleRules(n_period=2, fast_entry=3, slow_entry=5, fast_exit=2, slow_exit=3,
                        fast_system_enabled=False, skip_fast_after_win=False,
                        entry_confirmation_bars=3, stop_n=10.)
    spec = AssetSpec('BTC', 'crypto', 'c', qty_step=.001, min_qty=.001, can_short=False)
    engine = MultiAssetTurtleStrategy({'BTC': spec}, rules)
    return MonitorScanner(repository, EventStore(path, purpose=purpose), TurtleEvaluator(engine),
                          code_version=CodeVersion('d' * 40, 'e' * 64))


def current_time(repository):
    return (repository.load_bars('BTC', 'D1')['bar_end'].max() + pd.Timedelta(seconds=61)).to_pydatetime()


def test_repeated_identical_scan_is_not_recomputed_or_renotified(market, tmp_path):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    monitor.evaluator.evaluate = Mock(wraps=monitor.evaluator.evaluate)
    account = ReferenceAccount('paper', 100000.)
    first = monitor.scan('BTC', 'D1', now=current_time(repository), account=account)
    second = monitor.scan('BTC', 'D1', now=current_time(repository) + timedelta(minutes=1), account=account)
    assert first['created'] is True and second['created'] is False and second['skipped'] is True
    assert first['snapshot'] == second['snapshot']
    assert monitor.evaluator.evaluate.call_count == 1
    assert len(monitor.store.rows('decisions')) == 1
    assert len(monitor.store.rows('outbox')) == 2  # Market change and independent DATA_READY.


def test_repository_failure_records_health_and_never_market_signal(market, tmp_path, monkeypatch):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    from inv_trend.data.models import DataLineageError
    def fail(*args, **kwargs):
        raise DataLineageError('corrupt manifest')
    monkeypatch.setattr(repository, 'load_bars', fail)
    for minute in range(2):
        result = monitor.scan('BTC', 'H4', now=pd.Timestamp('2024-01-02T00:01Z').to_pydatetime() + timedelta(minutes=minute))
        assert result['snapshot']['conditions']['quality_gate'] is False
    assert monitor.store.state('BTC', 'H4').phase is Phase.IDLE
    assert [event['kind'] for event in monitor.store.rows('events')] == ['DATA_HEALTH']


def test_stale_verified_data_suppresses_confirmation(market, tmp_path):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    result = monitor.scan('BTC', 'D1', now=current_time(repository) + timedelta(days=3),
                          account=ReferenceAccount('paper', 100000.))
    assert result['snapshot']['conditions']['freshness_gate'] is False
    assert result['snapshot']['state_after'] == 'IDLE'
    assert result['snapshot']['event']['kind'] == 'DATA_HEALTH'


def test_replay_preserves_all_six_states_and_backtest_intents(market, tmp_path):
    repository, version = market
    monitor = scanner(repository, tmp_path / 'replay.sqlite3', purpose='replay')
    result = HistoricalReplay(monitor).run('BTC', 'D1', version, compare_backtest=True)
    phases = {snapshot['state_after'] for snapshot in result.decisions}
    assert phases == {phase.value for phase in Phase}
    assert result.comparison['compared_bars'] == 14
    assert result.comparison['uncompared_bars'] == 1
    assert result.comparison['mismatches'] == []
    assert monitor.store.rows('outbox') == []
    assert all(snapshot['dataset_version'] == version for snapshot in result.decisions)
    assert all('before' in snapshot['state_context'] for snapshot in result.decisions)


def test_replay_pins_repository_version_and_retries_return_original_snapshots(market, tmp_path, monkeypatch):
    repository, version = market
    monitor = scanner(repository, tmp_path / 'replay.sqlite3', purpose='replay')
    def forbidden(*args, **kwargs):
        raise AssertionError('replay must not resolve mutable current data')
    monkeypatch.setattr(repository, 'load_bars', forbidden)
    replay = HistoricalReplay(monitor)
    first = replay.run('BTC', 'D1', version, compare_backtest=True)
    second = replay.run('BTC', 'D1', version, compare_backtest=True)
    assert first.to_dict() == second.to_dict()
    assert len(monitor.store.rows('decisions')) == 15
    assert monitor.store.state('BTC', 'D1').revision == 15


def test_saved_decisions_reproduce_without_current_state_or_new_writes(market, tmp_path, monkeypatch):
    repository, version = market
    monitor = scanner(repository, tmp_path / 'replay.sqlite3', purpose='replay')
    trace = HistoricalReplay(monitor).run('BTC', 'D1', version, compare_backtest=True)
    original_state = monitor.store.state('BTC', 'D1').to_dict()
    def forbidden(*args, **kwargs):
        raise AssertionError('saved decision reproduction must not use current or commit')
    monkeypatch.setattr(repository, 'load_bars', forbidden)
    monkeypatch.setattr(monitor.store, 'commit', forbidden)
    monkeypatch.setattr(monitor.store, 'state', forbidden)
    for snapshot in trace.decisions:
        assert monitor.reproduce(snapshot) == snapshot
    assert len(monitor.store.rows('decisions')) == 15
    from inv_trend.monitoring.事件存储 import EventStore
    assert EventStore(monitor.store.path, purpose='replay').state('BTC', 'D1').to_dict() == original_state


def test_saved_decision_rejects_code_drift_and_tampered_evidence(market, tmp_path):
    from copy import deepcopy
    from inv_trend.data.models import DataLineageError
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    snapshot = monitor.scan('BTC', 'D1', now=current_time(repository),
                            account=ReferenceAccount('paper', 100000.))['snapshot']
    damaged = deepcopy(snapshot)
    damaged['indicators']['n'] = 999.
    with pytest.raises(ValueError, match='content hash'):
        monitor.reproduce(damaged)
    monitor.code = CodeVersion('d' * 40, 'f' * 64)
    with pytest.raises(DataLineageError, match='recorded code'):
        monitor.reproduce(snapshot)


@pytest.mark.parametrize('damage', ['symbol', 'timeframe', 'dataset_version', 'high', 'bar_end'])
def test_repository_boundary_rejects_misbound_or_invalid_bars(market, tmp_path, monkeypatch, damage):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    bars = repository.load_bars('BTC', 'D1')
    bars.loc[bars.index[-1], damage] = (
        -1. if damage == 'high' else bars['timestamp'].iloc[-1] if damage == 'bar_end' else 'incorrect')
    monkeypatch.setattr(repository, 'load_bars', lambda *args, **kwargs: bars)
    result = monitor.scan('BTC', 'D1', now=pd.Timestamp('2024-01-16T00:01:01Z').to_pydatetime())
    assert result['snapshot']['market'] is None
    assert result['snapshot']['event']['kind'] == 'DATA_HEALTH'
    assert monitor.store.state('BTC', 'D1').phase is Phase.IDLE


def test_reference_account_roundtrip_preserves_position_history_and_timestamps():
    from inv_trend.adapters.multi_asset.models.domain import SkippedFastTrade
    when = pd.Timestamp('2024-01-02T00:00Z')
    position = Position('BTC', 1, 'slow', [PositionUnit(2., 10., 1., when)], 10., 8.)
    state = PortfolioState({'BTC': position}, {'BTC': False},
                           {'BTC': SkippedFastTrade(-1, 10., 1., 12.)})
    account = ReferenceAccount('observed', 100000., state, {'BTC': 'position-1'}, 'observation-1', when)
    restored = ReferenceAccount.from_dict(account.to_dict())
    assert restored.to_dict() == account.to_dict() and restored.signature == account.signature
    assert restored.portfolio.positions['BTC'].first_entry_time == when


def test_real_h4_repository_replay_matches_backtest_and_saved_decisions(service, tmp_path):
    repository, version = market_repository(service, tmp_path / 'h4-market', timeframe='H4')
    monitor = scanner(repository, tmp_path / 'h4-replay.sqlite3', purpose='replay')
    trace = HistoricalReplay(monitor).run('BTC', 'H4', version, compare_backtest=True)
    assert {snapshot['state_after'] for snapshot in trace.decisions} == {phase.value for phase in Phase}
    assert trace.comparison['mismatches'] == [] and trace.comparison['compared_bars'] == 14
    assert all(snapshot['conditions']['freshness_gate'] is True for snapshot in trace.decisions)
    for snapshot in trace.decisions:
        assert monitor.reproduce(snapshot) == snapshot
    assert monitor.store.rows('outbox') == []


def test_future_price_changes_do_not_change_earlier_replayed_strategy_observations(service, tmp_path):
    repository_a, version_a = market_repository(service, tmp_path / 'causal-a')
    repository_b, version_b = market_repository(service, tmp_path / 'causal-b', future_prices=[10., 10.5, 10.2])
    first = HistoricalReplay(scanner(repository_a, tmp_path / 'a.sqlite3', purpose='replay')).run(
        'BTC', 'D1', version_a, account=ReferenceAccount('paper', 100000.))
    second = HistoricalReplay(scanner(repository_b, tmp_path / 'b.sqlite3', purpose='replay')).run(
        'BTC', 'D1', version_b, account=ReferenceAccount('paper', 100000.))
    assert version_a != version_b
    for left, right in zip(first.decisions[:12], second.decisions[:12], strict=True):
        for key in ('bar_time', 'market', 'indicators', 'conditions', 'state_before', 'state_after', 'event', 'reasons'):
            assert left[key] == right[key], key
        assert left['state_context']['intents'] == right['state_context']['intents']


def test_unfinished_observation_is_candidate_and_never_close_confirmed():
    engine = MultiAssetTurtleStrategy({'BTC': AssetSpec('BTC', 'crypto', 'c')},
                                     TurtleRules(n_period=2, fast_entry=3, slow_entry=5))
    evaluator = TurtleEvaluator(engine)
    frame = price_bars([10.] * 6 + [12.])
    frame['is_complete'] = True
    frame.loc[frame.index[-1], 'is_complete'] = False
    result = evaluator.evaluate(frame, 'BTC', 'H4', ReferenceAccount('paper', 100000.), account_known=True)
    assert result.candidate is True
    assert result.close_confirmed is False and result.execution_allowed is False
    assert result.intents == ()
    assert result.conditions['bar_complete'] is False


def test_observed_account_unknown_fast_trade_history_cannot_pass_execution_gate():
    engine = MultiAssetTurtleStrategy({'BTC': AssetSpec('BTC', 'crypto', 'c')},
                                     TurtleRules(n_period=2, fast_entry=3, slow_entry=5))
    frame = price_bars([10.] * 6 + [12.])
    frame['is_complete'] = True
    account = ReferenceAccount('observed', 100000., observation_id='observation-1',
                               observed_at=pd.Timestamp('2024-01-08T00:00Z').to_pydatetime())
    result = TurtleEvaluator(engine).evaluate(frame, 'BTC', 'D1', account, account_known=True)
    assert result.close_confirmed is True and result.execution_allowed is False
    assert result.conditions['fast_history_gate'] is False


def test_strategy_calculation_does_not_mutate_supplied_reference_portfolio():
    frame = price_bars([10.] * 6 + [12.])
    frame['is_complete'] = True
    engine = MultiAssetTurtleStrategy({'BTC': AssetSpec('BTC', 'crypto', 'c')},
                                     TurtleRules(n_period=2, fast_entry=3, slow_entry=5))
    original = PortfolioState(last_fast_trade_won={'BTC': True})
    account = ReferenceAccount('paper', 100000., original)
    signature = account.signature
    TurtleEvaluator(engine).evaluate(frame, 'BTC', 'D1', account, account_known=True)
    assert account.signature == signature
    assert original.skipped_fast_trades == {}


def test_observed_position_direction_is_independent_of_opposite_market_candidate():
    from inv_trend.monitoring.状态机 import Facts, transition
    from inv_trend.monitoring.模型 import StateRecord
    result = transition(StateRecord('BTC', 'D1'), Facts(False, True, False, True, False, True, True,
                        -1, position_open=True, position_ref='observed-long', position_direction=1))
    assert result.after.phase is Phase.POSITION and result.after.direction == 1


def test_stale_account_reference_is_unknown_and_future_reference_is_rejected():
    now = pd.Timestamp('2024-01-02T00:00Z').to_pydatetime()
    account = ReferenceAccount('observed', 100000., observation_id='observation-1',
                               observed_at=now - timedelta(minutes=10))
    assert account.is_known(now, 300.) is False
    assert replace(account, observed_at=now + timedelta(minutes=1)).is_known(now, 300.) is False
