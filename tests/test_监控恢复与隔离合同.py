"""实际进程退出、live/replay隔离和同次决策的独立事件。"""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import sqlite3

import pytest

from inv_trend.data.models import DataLineageError
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.回放 import HistoricalReplay
from inv_trend.monitoring.策略适配 import ReferenceAccount
from inv_trend.monitoring.模型 import Event, EventKind, Phase, StateRecord
from inv_trend.monitoring.状态机 import transition
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_监控状态与事务合同 import facts, decision
from tests.test_数据封版合同 import service as service


def test_health_recovery_and_market_transition_are_both_audited(market, tmp_path):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    now = current_time(repository)
    monitor.record_failure(repository.instrument('BTC'), 'D1', now=now,
                            error=DataLineageError('temporarily unavailable manifest'))
    monitor.scan('BTC', 'D1', now=now, account=ReferenceAccount('paper', 100000.))
    events = monitor.store.rows('events')
    kinds = {event['kind'] for event in events if event['code'] != 'MANIFEST_FAILURE'}
    assert kinds == {'MARKET_SIGNAL', 'DATA_HEALTH'}
    assert sum(event['code'] == 'DATA_RECOVERED' for event in events) == 1


def test_observed_position_and_same_bar_stop_exit_are_both_audited():
    outcome = transition(StateRecord('BTC', 'D1'), facts(position_open=True,
                         position_ref='observed-1', exit_triggered=True, exit_code='STOP_EXIT'))
    assert outcome.after.phase is Phase.EXIT and outcome.event.code == 'STOP_EXIT'
    assert [event.kind for event in outcome.additional_events] == [EventKind.POSITION_EVENT]
    assert outcome.additional_events[0].code == 'POSITION_OBSERVED'


def test_replay_cannot_adopt_live_event_store_even_when_empty(market, tmp_path):
    repository, version = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    with pytest.raises(ValueError, match='replay.*store'):
        HistoricalReplay(monitor).run('BTC', 'D1', version, compare_backtest=True)
    assert monitor.store.rows('decisions') == [] and monitor.store.rows('monitoring_state') == []


def test_persisted_store_purpose_cannot_be_changed_on_restart(tmp_path):
    path = tmp_path / 'replay.sqlite3'
    EventStore(path, purpose='replay')
    original = path.read_bytes()
    with pytest.raises(ValueError, match='purpose'):
        EventStore(path, purpose='live')
    assert path.read_bytes() == original


def test_different_failure_evidence_is_saved_without_repeating_health_event(market, tmp_path):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    now = current_time(repository)
    for message in ['missing report', 'report hash changed']:
        monitor.record_failure(repository.instrument('BTC'), 'D1', now=now, error=DataLineageError(message))
    assert len(monitor.store.rows('decisions')) == 2
    assert len(monitor.store.rows('events')) == len(monitor.store.rows('outbox')) == 1
    import json
    assert {json.loads(row['payload'])['context']['failure']['message']
            for row in monitor.store.rows('decisions')} == {'missing report', 'report hash changed'}


def test_external_account_mutation_cannot_change_bound_decision_inputs(market, tmp_path, monkeypatch):
    from inv_trend.adapters.multi_asset.models.domain import PortfolioState, Position, PositionUnit
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    position = Position('BTC', 1, 'slow', [PositionUnit(1., 10., 1.)], 10., 1.)
    account = ReferenceAccount('paper', 100000., PortfolioState({'BTC': position}), {'BTC': 'position-1'})
    original = monitor.evaluator.evaluate
    def concurrent_change(bars, symbol, frame, reference, **kwargs):
        account.portfolio.positions['BTC'].units[0].qty = 999.
        return original(bars, symbol, frame, reference, **kwargs)
    monkeypatch.setattr(monitor.evaluator, 'evaluate', concurrent_change)
    saved = monitor.scan('BTC', 'D1', now=current_time(repository), account=account)['snapshot']
    assert saved['context']['account']['portfolio']['positions']['BTC']['units'][0]['qty'] == 1.
    assert saved['state_context']['strategy_after']['positions']['BTC']['units'][0]['qty'] == 1.
    assert account.portfolio.positions['BTC'].total_qty == 999.
    monkeypatch.setattr(monitor.evaluator, 'evaluate', original)
    assert monitor.reproduce(saved) == saved


def test_snapshot_timestamps_do_not_truncate_nanosecond_evidence():
    before = StateRecord('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    precise = '2024-01-03T00:00:00.123456789+00:00'
    snapshot = decision(before, outcome, bar=precise, evaluated_at=precise)
    assert snapshot.bar_time == snapshot.evaluated_at == precise


def test_account_roundtrip_does_not_truncate_nanosecond_observations():
    import pandas as pd
    from inv_trend.adapters.multi_asset.models.domain import PortfolioState, Position, PositionUnit
    when = pd.Timestamp('2024-01-03T00:00:00.123456789Z')
    position = Position('BTC', 1, 'slow', [PositionUnit(1., 10., 1., when)], 10., 1.)
    original = ReferenceAccount('observed', 100000., PortfolioState({'BTC': position}),
                                {'BTC': 'position-1'}, 'observation-1', when)
    assert ReferenceAccount.from_dict(original.to_dict()).to_dict() == original.to_dict()


def test_second_event_failure_rolls_back_entire_decision_and_retries_once(tmp_path, monkeypatch):
    store = EventStore(tmp_path / 'live.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    snapshot = decision(before, outcome, additional_events=(Event(EventKind.DATA_HEALTH, 'DATA_READY'),))
    original = store._write_event
    def fail_second(db, snapshot, event=None):
        if event is not None:
            raise RuntimeError('secondary event insert fault')
        return original(db, snapshot)
    monkeypatch.setattr(store, '_write_event', fail_second)
    with pytest.raises(RuntimeError, match='secondary event'):
        store.commit(snapshot, before, outcome.after)
    recovered = EventStore(store.path)
    assert recovered.state('BTC', 'D1') == before
    for table in ('decisions', 'events', 'outbox', 'monitoring_state'):
        assert recovered.rows(table) == []
    recovered.commit(snapshot, before, outcome.after)
    recovered.commit(snapshot, before, outcome.after)
    assert len(recovered.rows('decisions')) == 1
    assert len(recovered.rows('events')) == len(recovered.rows('outbox')) == 2


@pytest.mark.parametrize('mode', ['live', None])
def test_legacy_role_migration_requires_verifiable_decisions_and_preserves_evidence(tmp_path, mode):
    path = tmp_path / 'legacy.sqlite3'
    store = EventStore(path)
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    snapshot = decision(before, outcome, context={'mode': mode} if mode else {})
    store.commit(snapshot, before, outcome.after)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE monitoring_mode')
        db.execute('PRAGMA user_version=1')
    original = path.read_bytes()
    if mode is None:
        with pytest.raises(ValueError, match='purpose cannot be verified'):
            EventStore(path)
        assert path.read_bytes() == original
    else:
        restored = EventStore(path)
        assert restored.state('BTC', 'D1') == store.state('BTC', 'D1')
        assert restored.rows('decisions')[0]['payload'] == store.rows('decisions')[0]['payload']
        with sqlite3.connect(path) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 3
            assert db.execute('SELECT purpose FROM monitoring_mode').fetchall() == [('live',)]


@pytest.mark.parametrize('case', ['mode', 'outbox'])
def test_replay_store_rejects_live_decisions_and_notification_enqueues(tmp_path, case):
    store = EventStore(tmp_path / 'replay.sqlite3', purpose='replay')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    snapshot = decision(before, outcome, context={'mode': 'live' if case == 'mode' else 'replay'})
    with pytest.raises(ValueError):
        store.commit(snapshot, before, outcome.after, enqueue=case == 'outbox')
    assert store.rows('decisions') == [] and store.rows('outbox') == []


@pytest.mark.parametrize('stage', ['before_commit', 'after_commit'])
@pytest.mark.parametrize('exit_style', ['crt', 'kernel'])
def test_real_process_exit_restores_atomic_commit_and_retry(tmp_path, stage, exit_style):
    if exit_style == 'kernel' and os.name != 'nt':
        pytest.skip('Windows TerminateProcess contract')
    path = tmp_path / 'events.sqlite3'
    script = '''
import ctypes, os, sys
from inv_trend.monitoring.事件存储 import EventStore
from tests.test_监控状态与事务合同 import facts, decision
from inv_trend.monitoring.状态机 import transition
store = EventStore(sys.argv[1])
stage, style = sys.argv[2:]
def crash():
    print('monitor-fault-reached', flush=True)
    if style == 'kernel':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel.TerminateProcess(kernel.GetCurrentProcess(), 74)
        raise RuntimeError('TerminateProcess unexpectedly returned')
    os._exit(74)
before = store.state('BTC', 'D1')
outcome = transition(before, facts(setup=True))
original = store._write_event
if stage == 'before_commit':
    def fail(db, snapshot, event=None):
        if event is None:
            original(db, snapshot)
        else:
            original(db, snapshot, event)
        crash()
    store._write_event = fail
store.commit(decision(before, outcome), before, outcome.after)
crash()
'''
    result = subprocess.run([sys.executable, '-c', script, str(path), stage, exit_style],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert result.returncode == 74, (result.returncode, result.stderr.decode(errors='replace'))
    assert b'monitor-fault-reached' in result.stdout
    restored = EventStore(path)
    count = int(stage == 'after_commit')
    assert restored.state('BTC', 'D1').revision == count
    for table in ('decisions', 'events', 'outbox', 'monitoring_state'):
        assert len(restored.rows(table)) == count, table
    before = StateRecord('BTC', 'D1')
    outcome = transition(before, facts(setup=True))
    snapshot = decision(before, outcome)
    retried = restored.commit(snapshot, before, outcome.after)
    assert retried['created'] is (stage == 'before_commit')
    twice = EventStore(path).commit(replace(snapshot, evaluated_at='2024-01-04T00:02:00+00:00'),
                                    before, outcome.after)
    assert twice['created'] is False and twice['snapshot'] == retried['snapshot']
    for table in ('decisions', 'events', 'outbox', 'monitoring_state'):
        assert len(restored.rows(table)) == 1
