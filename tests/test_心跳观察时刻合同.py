import pandas as pd
import pytest
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.健康 import HealthRecorder
from inv_trend.monitoring.扫描器 import CodeVersion


def create_health(tmp_path):
    ledger = RuntimeLedger(EventStore(tmp_path / 'events.sqlite3'), tmp_path / 'data')
    return ledger, HealthRecorder(ledger, code_version=CodeVersion('a' * 40, 'b' * 64), config_hash='c' * 64)


def test_real_pulse_arriving_during_read_does_not_emit_missing(tmp_path):
    ledger, health = create_health(tmp_path)
    start = pd.Timestamp('2026-10-04T10:42:32.123357Z')
    pulse = start + pd.Timedelta(microseconds=1423)
    times = iter([start, pulse + pd.Timedelta(microseconds=1)])
    original = ledger.heartbeats
    def read():
        ledger.pulse('scanner', 'KO:D1', now=pulse)
        return original()
    ledger.heartbeats = read
    result = health.check_heartbeats([('scanner', 'KO:D1', 600)], clock=lambda: next(times))
    assert result[0]['snapshot']['reasons'] == ['HEARTBEAT_RECOVERED']
    assert pd.Timestamp(result[0]['snapshot']['evaluated_at']) > pulse
    assert not any(x['code'] == 'HEARTBEAT_MISSING' for x in ledger.store.rows('events'))


def test_genuinely_future_pulse_is_still_missing(tmp_path):
    ledger, health = create_health(tmp_path)
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    ledger.pulse('scanner', 'BTC:D1', now=now + pd.Timedelta(seconds=30))
    result = health.check_heartbeats([('scanner', 'BTC:D1', 600)], clock=lambda: now)
    assert result[0]['snapshot']['reasons'] == ['HEARTBEAT_MISSING']


def test_clock_regression_during_read_does_not_commit(tmp_path):
    ledger, health = create_health(tmp_path)
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    times = iter([now, now - pd.Timedelta(microseconds=1)])
    with pytest.raises(ValueError, match='backward'):
        health.check_heartbeats([('scanner', 'BTC:D1', 600)], clock=lambda: next(times))
    assert ledger.store.rows('decisions') == []
    assert ledger.store.rows('events') == []


@pytest.mark.parametrize('args', [{}, {'now': pd.Timestamp('2024-01-01T00:00:00Z'), 'clock': lambda: None}])
def test_ambiguous_observation_time_is_rejected(tmp_path, args):
    _, health = create_health(tmp_path)
    with pytest.raises(ValueError, match='exactly one'):
        health.check_heartbeats([], **args)


def test_explicit_historical_cutoff_is_not_advanced(tmp_path):
    ledger, health = create_health(tmp_path)
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    ledger.pulse('scanner', 'BTC:D1', now=now + pd.Timedelta(microseconds=1))
    result = health.check_heartbeats([('scanner', 'BTC:D1', 600)], now=now)
    assert result[0]['snapshot']['reasons'] == ['HEARTBEAT_MISSING']
    assert pd.Timestamp(result[0]['snapshot']['evaluated_at']) == now
