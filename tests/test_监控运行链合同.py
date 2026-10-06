"""真实Repository运行链、任务审计、锁、超时及独立心跳合同。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event

import pandas as pd
import pytest

from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.健康 import HealthRecorder
from inv_trend.monitoring.策略适配 import ReferenceAccount
from inv_trend.monitoring.运行配置 import RuntimeConfig, Target, load_runtime_config
from inv_trend.monitoring.运行时 import MonitoringRuntime, ScanTimeoutError
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_数据封版合同 import service as service


def runtime(market, tmp_path, *, updater=None, **overrides):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'runtime.sqlite3')
    config = RuntimeConfig(data_root=repository.lake.root, runtime_dir=tmp_path,
                            symbols=('BTC',), timeframes=('D1',), paper_equity=100000.,
                            **({'refresh': False} | overrides))
    now = current_time(repository)
    return MonitoringRuntime(monitor, config, updater=updater, clock=lambda: now)


def test_run_once_uses_verified_repository_and_saves_operational_history(market, tmp_path):
    with runtime(market, tmp_path) as monitor:
        first = monitor.run_once()[0]
        second = monitor.run_once()[0]
        assert first['created'] is True and second['created'] is False
        assert first['snapshot'] == second['snapshot'] and first['job_id'] != second['job_id']
        jobs = monitor.ledger.tasks()
        assert len(jobs) == 2 and all(job['status'] == 'SUCCESS' for job in jobs)
        assert jobs[0]['decision_id'] == jobs[1]['decision_id']
        assert {(row['component'], row['scope']) for row in monitor.ledger.heartbeats()} == {
            ('scanner', 'BTC:D1'), ('scheduler', 'main'), ('lock', 'BTC:D1')}
        assert sum(event['kind'] == 'MARKET_SIGNAL' for event in monitor.scanner.store.rows('events')) == 1


def test_real_update_runs_before_repository_load(market, tmp_path, monkeypatch):
    repository, _ = market
    calls = []
    update, load = repository.update, repository.load_bars
    def update_spy(*args, **kwargs):
        calls.append('update')
        return update(*args, **kwargs)
    def load_spy(*args, **kwargs):
        calls.append('load')
        return load(*args, **kwargs)
    monkeypatch.setattr(repository, 'load_bars', load_spy)
    with runtime(market, tmp_path, updater=update_spy, refresh=True) as monitor:
        # Clock access is independent from Repository load in this ordering test.
        monitor.clock = lambda: pd.Timestamp('2024-01-16T00:01:01Z').to_pydatetime()
        calls.clear()  # Only observe the authorized runtime cycle, after fixture preparation.
        assert monitor.run_once()[0]['status'] == 'SUCCESS'
    assert calls.index('update') < calls.index('load')


def test_provider_failure_is_traced_and_never_uses_old_market_signal(market, tmp_path):
    def broken(*args, **kwargs):
        raise OSError('provider https://example.invalid/PRIVATE_TOKEN unreachable')
    with runtime(market, tmp_path, updater=broken, refresh=True) as monitor:
        result = monitor.run_once()[0]
        assert result['status'] == 'FAILED'
        assert result['snapshot']['event']['code'] == 'PROVIDER_FAILURE'
        assert result['snapshot']['market'] is None
        assert monitor.ledger.tasks()[0]['status'] == 'FAILED'
        assert all(event['kind'] != 'MARKET_SIGNAL' for event in monitor.scanner.store.rows('events'))
        assert 'PRIVATE_TOKEN' not in json.dumps(monitor.ledger.tasks())


def test_same_target_overlap_is_skipped_and_other_timeframe_has_independent_lock(market, tmp_path, monkeypatch):
    started, release = Event(), Event()
    with runtime(market, tmp_path) as monitor:
        original = monitor.scanner.evaluator.evaluate
        def slow(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(monitor.scanner.evaluator, 'evaluate', slow)
        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(monitor.scan_target, Target('BTC', 'D1'))
            try:
                assert started.wait(5)
                repeated = monitor.scan_target(Target('BTC', 'D1'))
                assert repeated['status'] == 'SKIPPED' and repeated['reason'] == 'LOCK_BUSY'
                other = monitor._target_lock(Target('BTC', 'H4'))
                with other:
                    assert other._acquired is True
                assert len(monitor.ledger.tasks()) == 1
            finally:
                release.set()
            assert future.result(timeout=5)['status'] == 'SUCCESS'


def test_scheduler_ticks_do_not_reenter_unfinished_job(market, tmp_path, monkeypatch):
    started, release = Event(), Event()
    with runtime(market, tmp_path, interval_seconds=.05, poll_seconds=.01) as monitor:
        elapsed = [0.]
        monitor.monotonic = lambda: elapsed[0]
        original = monitor.scanner.evaluator.evaluate
        def slow(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(monitor.scanner.evaluator, 'evaluate', slow)
        assert monitor.tick()['submitted'] == ['BTC:D1']
        try:
            assert started.wait(5)
            elapsed[0] = .06
            assert monitor.tick()['submitted'] == []
            assert len(monitor.ledger.tasks()) == 1
        finally:
            release.set()
        results = monitor.run_once()
        assert len(results) == 1 and results[0]['status'] == 'SUCCESS'


def test_scan_timeout_blocks_late_transaction_and_still_saves_health(market, tmp_path, monkeypatch):
    started, release = Event(), Event()
    with runtime(market, tmp_path, scan_timeout_seconds=.1, interval_seconds=1., poll_seconds=.01) as monitor:
        elapsed = [0.]
        monitor.monotonic = lambda: elapsed[0]
        original = monitor.scanner.evaluator.evaluate
        def slow(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(monitor.scanner.evaluator, 'evaluate', slow)
        monitor.tick()
        try:
            assert started.wait(5)
            elapsed[0] = .15
            monitor.tick()
            assert any(event['code'] == 'SCAN_TIMEOUT' for event in monitor.scanner.store.rows('events'))
        finally:
            release.set()
        result = monitor.run_once()[0]
        assert result['status'] == 'TIMED_OUT'
        events = monitor.scanner.store.rows('events')
        assert sum(event['code'] == 'SCAN_TIMEOUT' for event in events) == 1
        assert all(event['kind'] != 'MARKET_SIGNAL' for event in events)
        assert monitor.ledger.tasks()[0]['status'] == 'TIMED_OUT'


def test_health_status_cycles_notify_once_per_transition_and_heartbeat_can_be_checked_externally(tmp_path):
    from inv_trend.monitoring.扫描器 import CodeVersion
    store = EventStore(tmp_path / 'live.sqlite3')
    ledger = RuntimeLedger(store, tmp_path / 'data')
    health = HealthRecorder(ledger, code_version=CodeVersion('a' * 40, 'b' * 64), config_hash='c' * 64)
    now = pd.Timestamp('2024-01-01T00:00Z')
    for code in ['READY', 'FAILED', 'READY', 'READY']:
        health.observe('scheduler', 'main', code, now=now)
    assert len(store.rows('events')) == 3
    health.check_heartbeats([('scheduler', 'main', 10.)], now=now + pd.Timedelta(seconds=11))
    health.check_heartbeats([('scheduler', 'main', 10.)], now=now + pd.Timedelta(seconds=12))
    assert sum(event['code'] == 'HEARTBEAT_MISSING' for event in store.rows('events')) == 1
    ledger.pulse('scheduler', 'main', now=now + pd.Timedelta(seconds=13))
    health.check_heartbeats([('scheduler', 'main', 10.)], now=now + pd.Timedelta(seconds=14))
    assert sum(event['code'] == 'HEARTBEAT_RECOVERED' for event in store.rows('events')) == 1


def test_runtime_store_rejects_data_root_switch_and_keeps_original_database(tmp_path):
    store = EventStore(tmp_path / 'live.sqlite3')
    RuntimeLedger(store, tmp_path / 'data-a')
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match='data_root'):
        RuntimeLedger(store, tmp_path / 'data-b')
    assert store.path.read_bytes() == original


def test_runtime_rejects_an_actual_repository_root_mismatch(market, tmp_path):
    with runtime(market, tmp_path) as monitor:
        wrong = replace(monitor.config, data_root=tmp_path / 'other-data')
        with pytest.raises(ValueError, match='data root differs'):
            MonitoringRuntime(monitor.scanner, wrong)
        assert monitor.ledger.tasks() == []


def test_timeout_after_tentative_event_write_rolls_back_the_whole_decision(market, tmp_path, monkeypatch):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    attempted = []
    original = monitor.store._write_event
    def write_event(*args, **kwargs):
        result = original(*args, **kwargs)
        attempted.append(result)
        return result
    def guard():
        if attempted:
            raise ScanTimeoutError('deadline crossed before COMMIT')
    monkeypatch.setattr(monitor.store, '_write_event', write_event)
    with pytest.raises(ScanTimeoutError, match='before COMMIT'):
        monitor.scan('BTC', 'D1', now=current_time(repository),
                     account=ReferenceAccount('paper', 100000.), guard=guard)
    assert attempted
    for table in ('decisions', 'events', 'outbox'):
        assert monitor.store.rows(table) == []
    assert monitor.store.state('BTC', 'D1').revision == 0


def test_bookkeeping_failure_closes_job_and_releases_lock(market, tmp_path, monkeypatch):
    with runtime(market, tmp_path) as monitor:
        original = monitor.ledger.pulse
        def fail_started(component, scope, **kwargs):
            if component == 'scanner' and kwargs.get('details', {}).get('status') == 'RUNNING':
                raise OSError('heartbeat write failed')
            return original(component, scope, **kwargs)
        monkeypatch.setattr(monitor.ledger, 'pulse', fail_started)
        result = monitor.run_once()[0]
        assert result['status'] == 'FAILED'
        assert result['snapshot']['event']['kind'] == 'SYSTEM_HEALTH'
        assert result['snapshot']['event']['code'] == 'SCANNER_FAILURE'
        assert monitor.ledger.tasks()[0]['finished_at'] is not None
        assert all(event['kind'] != 'MARKET_SIGNAL' for event in monitor.scanner.store.rows('events'))
        monkeypatch.setattr(monitor.ledger, 'pulse', original)
        assert monitor.run_once()[0]['status'] == 'SUCCESS'
        assert len(monitor.ledger.tasks()) == 2


def test_scheduler_dispatch_failure_is_recorded_and_next_cycle_can_recover(market, tmp_path, monkeypatch):
    with runtime(market, tmp_path) as monitor:
        original = monitor._pool.submit
        def broken(*args, **kwargs):
            raise RuntimeError('executor dispatch failed')
        monkeypatch.setattr(monitor._pool, 'submit', broken)
        with pytest.raises(RuntimeError, match='dispatch failed'):
            monitor.tick()
        events = monitor.scanner.store.rows('events')
        assert any(event['kind'] == 'SYSTEM_HEALTH' and event['code'] == 'SCHEDULER_FAILURE' for event in events)
        assert monitor.ledger.tasks() == []
        monkeypatch.setattr(monitor._pool, 'submit', original)
        assert monitor.run_once()[0]['status'] == 'SUCCESS'


@pytest.mark.parametrize('liveness', [True, None])
def test_durable_job_is_not_stolen_when_owner_is_alive_or_unknown(tmp_path, monkeypatch, liveness):
    store = EventStore(tmp_path / 'live.sqlite3')
    first = RuntimeLedger(store, tmp_path / 'data')
    second = RuntimeLedger(store, tmp_path / 'data')
    now = pd.Timestamp('2024-01-01T00:00Z')
    job = first.begin('BTC', 'H4', now=now)
    monkeypatch.setattr(sys.modules[RuntimeLedger.__module__], '_owner_process_state', lambda owner: liveness)
    assert second.recover(now=now + pd.Timedelta(minutes=1)) == []
    with pytest.raises(sqlite3.IntegrityError):
        second.begin('BTC', 'H4', now=now + pd.Timedelta(minutes=1))
    with pytest.raises(ValueError, match='another runtime'):
        second.finish(job, now=now + pd.Timedelta(minutes=1), status='SUCCESS')
    assert first.tasks()[0]['finished_at'] is None


def test_kernel_exit_releases_target_lock_and_recovers_unfinished_job(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows kernel lock contract')
    from inv_trend.data.locking import FileLock
    db = tmp_path / 'live.sqlite3'
    root = tmp_path / 'data'
    lockpath = root / '.monitoring_locks' / 'target.lock'
    script = '''
import ctypes, sys
import pandas as pd
from pathlib import Path
from inv_trend.data.locking import FileLock
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
ledger = RuntimeLedger(EventStore(sys.argv[1]), sys.argv[2])
lock = FileLock(Path(sys.argv[3])); lock.acquire()
ledger.begin('BTC', 'H4', now=pd.Timestamp('2024-01-01T00:00Z'))
print('runtime-kernel-fault-reached', flush=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetCurrentProcess.restype = ctypes.c_void_p
kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
kernel.TerminateProcess(kernel.GetCurrentProcess(), 74)
raise RuntimeError('unexpected kernel return')
'''
    result = subprocess.run([sys.executable, '-c', script, str(db), str(root), str(lockpath)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert result.returncode == 74, result.stderr.decode(errors='replace')
    assert b'runtime-kernel-fault-reached' in result.stdout
    ledger = RuntimeLedger(EventStore(db), root)
    with FileLock(lockpath, timeout=.2):
        assert len(ledger.recover(now=pd.Timestamp('2024-01-01T00:01Z'))) == 1
        assert ledger.tasks()[0]['status'] == 'INTERRUPTED'
        job = ledger.begin('BTC', 'H4', now=pd.Timestamp('2024-01-01T00:01Z'))
        ledger.finish(job, now=pd.Timestamp('2024-01-01T00:02Z'), status='SUCCESS')


@pytest.mark.parametrize('override', [{'refresh': 'false'}, {'workers': True}, {'timeframes': ()},
                                    {'symbols': ('BTC', 'BTC')}, {'scan_timeout_seconds': float('nan')},
                                    {'paper_equity': 0.}, {'symbols': 'BTC'}, {'symbols': (True,)},
                                    {'symbols': (' BTC',)}, {'timeframes': 'D1'}])
def test_runtime_configuration_rejects_unknown_or_invalid_values(override):
    with pytest.raises((ValueError, TypeError)):
        RuntimeConfig(**override)


def test_configuration_paths_are_relative_to_the_yaml_and_strategy_rules_are_not_duplicated(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('data_root: ./data\nruntime_dir: ./runtime\nstrategy_config: ./strategy.yaml\n'
                    'symbols: [BTC, ETH]\ntimeframes: [1D, 4H]\nrefresh: false\n', encoding='utf-8')
    config = load_runtime_config(path)
    assert config.data_root == (tmp_path / 'data').resolve()
    assert config.strategy_config == (tmp_path / 'strategy.yaml').resolve()
    assert config.timeframes == ('D1', 'H4')
    assert config.refresh is False
    path.write_text('rules: {}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='unsupported'):
        load_runtime_config(path)
