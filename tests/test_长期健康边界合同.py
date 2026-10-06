"""Role binding and first-missing components must remain fail closed."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest
import yaml
import pandas as pd

from inv_trend.cli.监控运行 import main
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.健康 import HealthRecorder
from inv_trend.monitoring.扫描器 import CodeVersion
from inv_trend.monitoring.看板 import DashboardReader
from inv_trend.monitoring.运行配置 import RuntimeConfig
from tests.test_监控运行链合同 import runtime
from tests.test_监控扫描与回放合同 import market as market, current_time
from tests.test_数据封版合同 import service as service


def test_dashboard_refuses_existing_runtime_store_bound_to_another_root(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    RuntimeLedger(store, tmp_path / 'original')
    with pytest.raises(ValueError, match='data root'):
        DashboardReader(store, RuntimeConfig(data_root=tmp_path / 'other'))


def test_watchdog_checks_configured_scanner_before_any_first_pulse(tmp_path, capsys):
    config = RuntimeConfig(data_root=tmp_path / 'data', runtime_dir=tmp_path / 'runtime',
                           symbols=('BTC',), timeframes=('D1','H4'))
    path = tmp_path / 'runtime.yaml'
    path.write_text(yaml.safe_dump(config.to_dict()), encoding='utf-8')
    assert main(['--config',str(path),'watchdog']) == 2
    assert json.loads(capsys.readouterr().out)['checks'].count('HEARTBEAT_MISSING') == 3


def test_healthy_pulse_changes_do_not_grow_immutable_health_decisions(tmp_path):
    import pandas as pd
    ledger = RuntimeLedger(EventStore(tmp_path / 'events.sqlite3'), tmp_path / 'data')
    health = HealthRecorder(ledger, code_version=CodeVersion('a'*40,'b'*64), config_hash='c'*64)
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    for step in range(10):
        clock = now + timedelta(seconds=step)
        ledger.pulse('scheduler','main',now=clock)
        health.check_heartbeats([('scheduler','main',30)],now=clock)
    assert len(ledger.store.rows('events')) == 1
    assert len(ledger.store.rows('decisions')) == 1


def test_consecutive_provider_failures_survive_restart_and_emit_once_per_cycle(market, tmp_path):
    def broken(*args, **kwargs):
        raise OSError('provider unavailable')
    with runtime(market, tmp_path, updater=broken, refresh=True) as monitor:
        for _ in range(3):
            assert monitor.run_once()[0]['status'] == 'FAILED'
        assert sum(row['code'] == 'CONSECUTIVE_SCAN_FAILURES' for row in monitor.scanner.store.rows('events')) == 1
    with runtime(market, tmp_path, updater=broken, refresh=True) as restarted:
        assert restarted.run_once()[0]['status'] == 'FAILED'
        assert sum(row['code'] == 'CONSECUTIVE_SCAN_FAILURES' for row in restarted.scanner.store.rows('events')) == 1
        restarted.config = replace(restarted.config, refresh=False)
        assert restarted.run_once()[0]['status'] == 'SUCCESS'
        assert any(row['code'] == 'SCAN_FAILURES_RECOVERED' for row in restarted.scanner.store.rows('events'))


def test_runtime_registers_all_expected_targets_before_scan(market, tmp_path):
    with runtime(market, tmp_path) as monitor:
        assert monitor.ledger.expected_components() == [
            ('scanner','BTC:D1',monitor.config.interval_seconds + monitor.config.scan_timeout_seconds +
             monitor.config.heartbeat_timeout_seconds),
            ('scheduler','main',monitor.config.heartbeat_timeout_seconds)]
        health = monitor.health.check_heartbeats(monitor.ledger.expected_components(),now=current_time(market[0]))
        assert all(item['snapshot']['reasons'] == ['HEARTBEAT_STARTING'] for item in health)
        assert monitor.ledger.heartbeats() == []
        deadline = max(age for _, _, age in monitor.ledger.expected_components())
        expired = monitor.health.check_heartbeats(monitor.ledger.expected_components(),
            now=current_time(market[0]) + timedelta(seconds=deadline + 1))
        assert all(item['snapshot']['reasons'] == ['HEARTBEAT_MISSING'] for item in expired)


def test_restart_does_not_extend_first_heartbeat_grace(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    ledger = RuntimeLedger(store, tmp_path / 'data')
    ledger.expect('scanner', 'BTC:D1', 30, now=now)
    restarted = RuntimeLedger(EventStore(store.path), tmp_path / 'data')
    restarted.expect('scanner', 'BTC:D1', 30, now=now + timedelta(seconds=31))
    health = HealthRecorder(restarted, code_version=CodeVersion('a'*40, 'b'*64), config_hash='c'*64)
    missing = health.check_heartbeats(restarted.expected_components(), now=now + timedelta(seconds=31))
    assert missing[0]['snapshot']['reasons'] == ['HEARTBEAT_MISSING']
    restarted.pulse('scanner', 'BTC:D1', now=now + timedelta(seconds=32))
    recovered = health.check_heartbeats(restarted.expected_components(), now=now + timedelta(seconds=32))
    assert recovered[0]['snapshot']['reasons'] == ['HEARTBEAT_RECOVERED']


def test_notification_is_expected_before_first_work_and_idle_worker_pulses(tmp_path):
    from inv_trend.notifications.队列 import NotificationWorker
    from tests.test_通知事务队列合同 import Channel
    ledger = RuntimeLedger(EventStore(tmp_path / 'events.sqlite3'), tmp_path / 'data')
    health = HealthRecorder(ledger, code_version=CodeVersion('a'*40,'b'*64), config_hash='c'*64)
    clock = [pd.Timestamp('2024-01-01T00:00:00Z')]
    worker = NotificationWorker(ledger.store, Channel(), health=health, clock=lambda: clock[0],
                                heartbeat_timeout_seconds=30)
    assert ledger.expected_components() == [('notification', 'test-channel', 30)]
    assert ledger.heartbeats() == []
    clock[0] += timedelta(seconds=31)
    assert health.check_heartbeats(ledger.expected_components(), now=clock[0])[0]['snapshot']['reasons'] == ['HEARTBEAT_MISSING']
    assert worker.run_once()['accepted']
    assert worker.run_once() is None
    assert health.check_heartbeats(ledger.expected_components(), now=clock[0])[0]['snapshot']['reasons'] == ['HEARTBEAT_RECOVERED']


def test_dashboard_refuses_repository_from_different_root_without_writing_store(market, tmp_path):
    from hashlib import sha256
    repository, _ = market
    store = EventStore(tmp_path / 'view.sqlite3')
    original = sha256(store.path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='Repository'):
        DashboardReader(store, RuntimeConfig(data_root=tmp_path / 'different'), repository)
    assert sha256(store.path.read_bytes()).hexdigest() == original


@pytest.mark.parametrize('age', [True, 0, -1, float('nan'), float('inf')])
def test_nonfinite_heartbeat_limits_never_report_healthy(tmp_path, age):
    ledger = RuntimeLedger(EventStore(tmp_path / 'events.sqlite3'), tmp_path / 'data')
    health = HealthRecorder(ledger, code_version=CodeVersion('a'*40,'b'*64), config_hash='c'*64)
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    with pytest.raises(ValueError, match='finite'):
        ledger.expect('scanner', 'BTC:D1', age, now=now)
    with pytest.raises(ValueError, match='finite'):
        health.check_heartbeats([('scanner', 'BTC:D1', age)], now=now)
