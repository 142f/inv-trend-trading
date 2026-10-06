"""Delivery survives contention, retry and worker death without altering decisions."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.状态机 import transition
from inv_trend.notifications.渠道 import DeliveryReceipt, NotificationError
from inv_trend.notifications.渠道 import WebhookNotifier
from inv_trend.notifications.队列 import NotificationPolicy, NotificationQueue, NotificationWorker
from tests.test_监控状态与事务合同 import decision, facts
from tests.test_通知渠道合同 import receiver
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_数据封版合同 import service as service


NOW = '2024-01-04T00:02:00+00:00'


def seed(tmp_path, *, setup=False):
    store = EventStore(tmp_path / 'events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(setup=True) if setup else facts(close_confirmed=True))
    snap = decision(before, outcome)
    store.commit(snap, before, outcome.after)
    return store, snap


class Channel:
    channel_id = 'test-channel'
    timeout_seconds = 1

    def __init__(self, error=None):
        self.sent, self.error = [], error

    def send(self, envelope):
        self.sent.append(envelope)
        if self.error:
            raise self.error
        return DeliveryReceipt(self.channel_id, envelope['event_id'], 204)


def claim(queue, now=NOW):
    return queue.claim('test-channel', now=now, lease_seconds=30,
                       policy=NotificationPolicy(), max_attempts=3)


def test_concurrent_workers_claim_and_send_exactly_one_local_attempt(tmp_path):
    store, snap = seed(tmp_path)
    channel = Channel()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: NotificationWorker(store, channel, clock=lambda: NOW).run_once(), range(8)))
    assert sum(item is not None for item in results) == 1
    assert len(channel.sent) == 1
    assert store.rows('outbox')[0]['status'] == 'DELIVERED'
    envelope = channel.sent[0]
    assert envelope['snapshot_id'] == snap.decision_id
    assert envelope['dedup_key'] == envelope['event_id']
    assert 'state_context' not in envelope and 'context' not in envelope
    assert store.rows('notification_attempts')[0]['http_status'] == 204
    assert store.rows('decisions')[0]['payload'] == json.dumps(
        json.loads(store.rows('decisions')[0]['payload']), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def test_retry_is_delayed_audited_and_keeps_same_remote_identity(tmp_path):
    store, _ = seed(tmp_path)
    channel = Channel(NotificationError('HTTP 503', retryable=True, http_status=503))
    now = [NOW]
    worker = NotificationWorker(store, channel, clock=lambda: now[0], retry_seconds=5)
    worker.run_once()
    assert store.rows('outbox')[0]['status'] == 'RETRY'
    assert worker.run_once() is None
    now[0] = '2024-01-04T00:02:05+00:00'
    channel.error = None
    worker.run_once()
    assert len(channel.sent) == 2
    assert channel.sent[0]['event_id'] == channel.sent[1]['event_id']
    assert [row['status'] for row in store.rows('notification_attempts')] == ['DELIVERED', 'RETRY']
    assert store.rows('outbox')[0]['delivered_at'] == now[0]


@pytest.mark.parametrize('error', [NotificationError('HTTP 400', retryable=False), RuntimeError('adapter bug')])
def test_permanent_failure_is_durable_and_scan_can_still_commit(tmp_path, error):
    store, snap = seed(tmp_path)
    channel = Channel(error)
    worker = NotificationWorker(store, channel, clock=lambda: NOW)
    worker.run_once()
    assert worker.run_once() is None
    assert store.rows('outbox')[0]['status'] == 'FAILED'
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts())
    changed = decision(before, outcome, bar='2024-01-04T00:00:00+00:00', dataset_version='data-2')
    assert store.commit(changed, before, outcome.after)['created']
    assert store.decision_for_input(snap.input_key)['decision_id'] == snap.decision_id


def test_expired_owner_cannot_finish_reclaimed_lease(tmp_path):
    store, _ = seed(tmp_path)
    queue = NotificationQueue(store)
    first = claim(queue)
    later = '2024-01-04T00:02:31+00:00'
    second = claim(queue, later)
    receipt = DeliveryReceipt('test-channel', first['envelope']['event_id'], 200)
    assert second['lease_token'] != first['lease_token']
    assert not queue.finish(first, now=later, receipt=receipt, max_attempts=3, retry_seconds=5)
    assert queue.finish(second, now=later, receipt=receipt, max_attempts=3, retry_seconds=5)
    assert [row['status'] for row in store.rows('notification_attempts')] == ['DELIVERED', 'LEASE_EXPIRED']


def test_crash_window_remote_dedup_retains_event_id_after_lease_expiry(tmp_path):
    store, _ = seed(tmp_path)
    queue = NotificationQueue(store)
    first = claim(queue)
    # Receiver accepts, worker dies before local finish. Receiver must dedup.
    receiver = {first['envelope']['dedup_key']}
    second = claim(NotificationQueue(EventStore(store.path)), '2024-01-04T00:02:31+00:00')
    receiver.add(second['envelope']['dedup_key'])
    assert len(receiver) == 1 and second['attempt'] == 2


def test_setup_policy_suppresses_without_network_attempt(tmp_path):
    store, _ = seed(tmp_path, setup=True)
    channel = Channel()
    assert NotificationWorker(store, channel, clock=lambda: NOW).run_once() is None
    assert channel.sent == [] and store.rows('notification_attempts') == []
    assert store.rows('outbox')[0]['status'] == 'SUPPRESSED'


def test_retry_budget_exhaustion_stops_future_attempts(tmp_path):
    store, _ = seed(tmp_path)
    channel = Channel(NotificationError('timeout', retryable=True))
    now = [NOW]
    worker = NotificationWorker(store, channel, clock=lambda: now[0], max_attempts=2)
    worker.run_once()
    now[0] = '2024-01-04T00:03:00+00:00'
    worker.run_once()
    assert worker.run_once() is None
    assert len(channel.sent) == 2 and store.rows('outbox')[0]['status'] == 'FAILED'


def test_version_two_upgrade_preserves_pending_decision_and_notification(tmp_path):
    store, snap = seed(tmp_path)
    original = store.rows('decisions')
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TABLE notification_attempts')
        for column in ('channel_id', 'lease_token', 'delivered_at'):
            db.execute(f'ALTER TABLE outbox DROP COLUMN {column}')
        db.execute('PRAGMA user_version=2')
    upgraded = EventStore(store.path)
    assert upgraded.rows('decisions') == original
    assert upgraded.decision_for_input(snap.input_key)['decision_id'] == snap.decision_id
    channel = Channel()
    NotificationWorker(upgraded, channel, clock=lambda: NOW).run_once()
    assert upgraded.rows('outbox')[0]['status'] == 'DELIVERED'
    assert EventStore(store.path).rows('outbox') == upgraded.rows('outbox')


def test_replay_store_and_unsafe_timing_rejected(tmp_path):
    with pytest.raises(ValueError, match='replay'):
        NotificationQueue(EventStore(tmp_path / 'replay.sqlite3', purpose='replay'))
    store, _ = seed(tmp_path)
    with pytest.raises(ValueError, match='lease'):
        NotificationWorker(store, Channel(), lease_seconds=1)
    with pytest.raises(ValueError, match='boolean'):
        NotificationPolicy(setup='false')


def test_real_http_pipeline_ack_and_duplicate_decision_send_once(tmp_path):
    store, snap = seed(tmp_path)
    before = store.state('BTC', 'D1')
    with receiver() as (url, received):
        worker = NotificationWorker(store, WebhookNotifier(url), clock=lambda: NOW)
        assert worker.run_once()['accepted']
        assert worker.run_once() is None
        # Idempotent re-commit must not reopen a delivered outbox entry.
        from inv_trend.monitoring.模型 import StateRecord
        original = StateRecord('BTC', 'D1')
        outcome = transition(original, facts(close_confirmed=True))
        assert not store.commit(snap, original, outcome.after)['created']
        assert worker.run_once() is None
        assert len(received) == 1 and received[0]['identity'] == received[0]['body']['event_id']
        assert store.rows('outbox')[0]['status'] == 'DELIVERED'
        assert store.state('BTC', 'D1') == before


def test_notify_cli_does_not_construct_repository_and_redacts_destination(tmp_path, monkeypatch, capsys):
    from inv_trend.cli.监控运行 import main
    import importlib
    module = importlib.import_module(main.__module__)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    store, _ = seed(tmp_path)
    # Move only this test's freshly created database after closing connections.
    import shutil
    shutil.copyfile(store.path, runtime / '监控事件.sqlite3')
    monkeypatch.setattr(module, 'build_scanner', lambda *a, **kw: pytest.fail('notify must not construct Repository'))
    with receiver() as (url, received):
        monkeypatch.setenv('MONITOR_TEST_WEBHOOK', url)
        assert main(['--runtime-dir', str(runtime), '--data-root', str(tmp_path / 'data'),
                     'notify', '--webhook-env', 'MONITOR_TEST_WEBHOOK']) == 0
        assert len(received) == 1
        assert 'PRIVATE_WEBHOOK_TOKEN' not in capsys.readouterr().out
        assert EventStore(runtime / '监控事件.sqlite3').rows('notification_attempts')[0]['status'] == 'DELIVERED'


def test_runtime_notification_configuration_is_explicit_and_immutable():
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    config = RuntimeConfig(notifications={'setup': True, 'candidate': False})
    assert config.to_dict()['notifications'] == {'setup': True, 'candidate': False}
    with pytest.raises(TypeError):
        config.notifications['setup'] = False
    with pytest.raises(ValueError):
        RuntimeConfig(notifications={'setup': 'false'})


def _crash_after_remote_ack(path, url):
    import os
    queue = NotificationQueue(EventStore(path))
    item = queue.claim(WebhookNotifier(url).channel_id, now=NOW, lease_seconds=30,
                       policy=NotificationPolicy(), max_attempts=3)
    WebhookNotifier(url).send(item['envelope'])
    os._exit(23)


def test_real_process_death_after_http_ack_recovers_with_same_remote_key(tmp_path):
    import multiprocessing
    store, _ = seed(tmp_path)
    with receiver() as (url, received):
        child = multiprocessing.get_context('spawn').Process(target=_crash_after_remote_ack,
                                                             args=(str(store.path), url))
        child.start()
        child.join(20)
        try:
            assert not child.is_alive() and child.exitcode == 23
            assert len(received) == 1
            assert store.rows('outbox')[0]['status'] == 'IN_FLIGHT'
            worker = NotificationWorker(EventStore(store.path), WebhookNotifier(url),
                                        clock=lambda: '2024-01-04T00:02:31+00:00')
            assert worker.run_once()['accepted']
            assert len(received) == 2
            assert len({row['identity'] for row in received}) == 1
            assert store.rows('outbox')[0]['status'] == 'DELIVERED'
            assert [row['status'] for row in store.rows('notification_attempts')] == ['DELIVERED', 'LEASE_EXPIRED']
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)


def test_notification_health_failure_and_recovery_do_not_feed_delivery_loop(tmp_path):
    from inv_trend.monitoring.执行记录 import RuntimeLedger
    from inv_trend.monitoring.健康 import HealthRecorder
    from inv_trend.monitoring.扫描器 import CodeVersion
    store, _ = seed(tmp_path)
    ledger = RuntimeLedger(store, tmp_path / 'data')
    health = HealthRecorder(ledger, code_version=CodeVersion('b' * 40, 'c' * 64), config_hash='a' * 64)
    now = [NOW]
    channel = Channel(NotificationError('https://example.invalid/SECRET_TOKEN HTTP 503', retryable=True))
    worker = NotificationWorker(store, channel, health=health, clock=lambda: now[0])
    worker.run_once()
    # A failure event exists, but cannot repeatedly notify through its failing channel.
    assert worker.run_once() is None
    now[0] = '2024-01-04T00:02:05+00:00'
    channel.error = None
    worker.run_once()
    assert worker.run_once() is None
    assert len(channel.sent) == 2
    assert {row['code'] for row in store.rows('events') if row['kind'] == 'NOTIFICATION_HEALTH'} == {
        'NOTIFICATION_FAILURE', 'NOTIFICATION_RECOVERED'}
    assert {row['status'] for row in store.rows('outbox')} == {'DELIVERED', 'SUPPRESSED'}
    assert 'SECRET_TOKEN' not in json.dumps(store.rows('decisions') + store.rows('notification_attempts'))
    assert any(row['component'] == 'notification' for row in ledger.heartbeats())


def test_schema_three_missing_columns_fails_without_repairing_unknown_shape(tmp_path):
    store, _ = seed(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute('ALTER TABLE outbox DROP COLUMN delivered_at')
    with pytest.raises(ValueError, match='schema is incomplete'):
        EventStore(store.path)
    with sqlite3.connect(store.path) as db:
        assert 'delivered_at' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}


def test_repository_strategy_snapshot_outbox_http_pipeline_is_shared_and_idempotent(market, tmp_path):
    from inv_trend.monitoring.策略适配 import ReferenceAccount
    repository, version = market
    monitor = scanner(repository, tmp_path / 'pipeline.sqlite3')
    now = current_time(repository)
    account = ReferenceAccount('paper', 100000.)
    result = monitor.scan('BTC', 'D1', now=now, account=account)
    assert result['snapshot']['dataset_version'] == version
    with receiver() as (url, received):
        worker = NotificationWorker(monitor.store, WebhookNotifier(url), clock=lambda: now.isoformat())
        delivered = worker.run_once()
        assert delivered and delivered['accepted']
        assert worker.run_once() is None
        assert not monitor.scan('BTC', 'D1', now=now, account=account)['created']
        assert worker.run_once() is None
        assert len(received) == 1
        assert received[0]['body']['snapshot_id'] == result['snapshot']['decision_id']
        assert received[0]['body']['event_type'] == 'MARKET_SIGNAL'
        assert {row['status'] for row in monitor.store.rows('outbox')} == {'DELIVERED', 'SUPPRESSED'}
