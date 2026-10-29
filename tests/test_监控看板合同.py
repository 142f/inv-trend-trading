"""HTTP views must show recorded evidence without reactivating blocked signals."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.回放记录 import ReplaySessionReader, ReplaySessionStore
from inv_trend.monitoring.运行配置 import RuntimeConfig
from inv_trend.monitoring.看板 import DashboardReader, create_app
from inv_trend.monitoring.策略适配 import ReferenceAccount
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_数据封版合同 import service as service, change_report


def view(market, tmp_path, replay=None):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    now = current_time(repository)
    ledger = RuntimeLedger(monitor.store, repository.lake.root)
    job = ledger.begin('BTC', 'D1', now=now)
    result = monitor.scan('BTC', 'D1', now=now, account=ReferenceAccount('paper', 100000.))
    ledger.finish(job, now=now, status='SUCCESS', decision_id=result['snapshot']['decision_id'])
    config = RuntimeConfig(data_root=repository.lake.root, runtime_dir=tmp_path,
                           symbols=('BTC',), timeframes=('D1','H4'), refresh=False)
    clock = [now]
    reader = DashboardReader(monitor.store, config, repository, clock=lambda: clock[0], replay=replay)
    return reader, monitor, clock


def test_dashboard_shows_all_required_fields_and_detail_without_mutations(market, tmp_path):
    reader, monitor, _ = view(market, tmp_path)
    before = hashlib.sha256(monitor.store.path.read_bytes()).hexdigest()
    client = TestClient(create_app(reader))
    model = client.get('/api/status').json()
    assert len(model['targets']) == 2
    row = model['targets'][0]
    assert {'asset','timeframe','data_health','trend','current_signal','monitoring_state',
            'last_bar','last_scan','last_event','snapshot_id','dataset_version'} <= row.keys()
    assert model['targets'][1]['data_health'] == 'DATA_MISSING'
    detail = client.get('/api/targets/BTC/D1').json()
    assert detail['metadata']['manifest']['dataset_version'] == detail['dataset_version']
    assert detail['metadata']['quality_report']['backtest_suitable'] is True
    assert detail['run_history'][0]['status'] == 'SUCCESS'
    assert detail['snapshot']['market'] and detail['snapshot']['conditions']
    assert client.get('/').status_code == 200
    assert client.get('/targets/BTC/D1').status_code == 200
    assert client.get('/r/targets/BTC/D1').status_code == 200
    assert hashlib.sha256(monitor.store.path.read_bytes()).hexdigest() == before
    assert client.post('/api/status').status_code == 405
    assert client.get('/api/targets/UNKNOWN/D1').status_code == 404
    assert client.get('/api/targets/BTC/H1').status_code == 404


def test_dashboard_read_connection_rejects_writes(market, tmp_path):
    reader, monitor, _ = view(market, tmp_path)
    before = monitor.store.state('BTC', 'D1')
    with reader._read() as db:
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute('UPDATE monitoring_state SET revision = revision + 1')
    assert monitor.store.state('BTC', 'D1') == before


def test_dashboard_rechecks_current_quality_and_blocks_previous_market_state(market, tmp_path):
    reader, monitor, _ = view(market, tmp_path)
    original = monitor.store.state('BTC','D1')
    change_report(reader.repository, lambda report: report.update(backtest_suitable=False))
    row = TestClient(create_app(reader)).get('/api/status').json()['targets'][0]
    assert row['data_health'] == 'QUALITY_FAILURE' and row['current_signal'] == 'BLOCKED'
    assert row['monitoring_state'] == original.phase.value
    assert monitor.store.state('BTC','D1') == original


def test_dashboard_clock_staleness_and_running_job_cannot_show_current_signal(market, tmp_path):
    reader, monitor, clock = view(market, tmp_path)
    clock[0] += timedelta(days=3)
    row = reader.overview()['targets'][0]
    assert row['data_health'] == 'STALE_DATA' and row['current_signal'] == 'BLOCKED'
    clock[0] -= timedelta(days=3)
    RuntimeLedger(monitor.store, reader.repository.lake.root).begin('BTC','D1', now=clock[0])
    row = reader.overview()['targets'][0]
    assert row['current_signal'] == 'BLOCKED' and 'SCAN_NOT_CURRENT' in row['blockers']


def test_unavailable_repository_still_serves_owned_health_store(tmp_path):
    config = RuntimeConfig(symbols=('BTC',), timeframes=('D1',), runtime_dir=tmp_path)
    reader = DashboardReader(EventStore(tmp_path / 'events.sqlite3'), config,
                             repository_error='https://example.invalid/PRIVATE_TOKEN failed')
    client = TestClient(create_app(reader))
    response = client.get('/')
    assert response.status_code == 200 and 'PRIVATE_TOKEN' not in response.text
    assert client.get('/api/status').json()['targets'][0]['current_signal'] == 'BLOCKED'
    assert response.headers['cache-control'] == 'no-store'


def test_dashboard_escapes_recorded_html_and_refuses_corrupt_snapshot(market, tmp_path):
    reader, monitor, _ = view(market, tmp_path)
    with monitor.store._connection() as db:
        row = db.execute('SELECT input_key,payload FROM decisions WHERE symbol=?', ('BTC',)).fetchone()
        payload = json.loads(row['payload'])
        payload['reasons'].append('<script>alert(1)</script>')
        db.execute('UPDATE decisions SET payload=? WHERE input_key=?', (json.dumps(payload), row['input_key']))
    client = TestClient(create_app(reader))
    result = client.get('/api/status').json()['targets'][0]
    assert result['current_signal'] == 'BLOCKED' and 'SNAPSHOT_FAILURE' in result['blockers']
    assert '<script>alert(1)</script>' not in client.get('/targets/BTC/D1').text


def test_confirmed_display_rechecks_the_recorded_account_observation_deadline(market, tmp_path):
    from inv_trend.monitoring.状态机 import transition
    from tests.test_监控状态与事务合同 import decision, facts
    repository, version = market
    now = current_time(repository)
    account = ReferenceAccount('observed', 100000., observation_id='observed-1', observed_at=now)
    store = EventStore(tmp_path / 'account-events.sqlite3')
    before = store.state('BTC', 'D1')
    outcome = transition(before, facts(close_confirmed=True))
    snap = decision(before, outcome, dataset_version=version,
                    instrument_id=repository.instrument('BTC').instrument_id,
                    bar=repository.load_bars('BTC', 'D1')['timestamp'].max().isoformat(),
                    evaluated_at=now.isoformat(),
                    context={'mode':'live', 'account': account.to_dict(), 'account_max_age_seconds':300.})
    from dataclasses import replace
    after = replace(outcome.after, details={'data_health':'FRESH'})
    store.commit(snap, before, after)
    ledger = RuntimeLedger(store, repository.lake.root)
    job = ledger.begin('BTC','D1', now=now)
    ledger.finish(job, now=now, status='SUCCESS', decision_id=snap.decision_id)
    config = RuntimeConfig(data_root=repository.lake.root, runtime_dir=tmp_path,
                           symbols=('BTC',),timeframes=('D1',),refresh=False)
    clock = [now]
    reader = DashboardReader(store, config, repository, clock=lambda:clock[0])
    assert reader.overview()['targets'][0]['current_signal'] == 'CONFIRMED_ENTRY'
    clock[0] += timedelta(seconds=301)
    row = reader.overview()['targets'][0]
    assert row['monitoring_state'] == 'CONFIRMED_ENTRY' and row['current_signal'] == 'BLOCKED'
    assert 'ACCOUNT_OBSERVATION_UNKNOWN' in row['blockers']


def test_validly_rehashed_snapshot_for_another_instrument_is_blocked(market, tmp_path):
    from dataclasses import replace
    from inv_trend.monitoring.模型 import DecisionSnapshot
    reader, monitor, _ = view(market, tmp_path)
    with monitor.store._connection() as db:
        row = db.execute('SELECT input_key,payload FROM decisions WHERE symbol=?', ('BTC',)).fetchone()
        saved = DecisionSnapshot.from_dict(json.loads(row['payload']))
        changed = replace(saved, instrument_id='ETHUSDT.BINANCE.SPOT')
        db.execute('UPDATE decisions SET payload=? WHERE input_key=?',
                   (json.dumps(changed.to_dict() | {'decision_id':changed.decision_id,
                                                  'input_key':changed.input_key}), row['input_key']))
    result = reader.overview()['targets'][0]
    assert result['current_signal'] == 'BLOCKED' and 'SNAPSHOT_FAILURE' in result['blockers']


def test_dashboard_filters_and_namespaced_api(market, tmp_path):
    reader, _monitor, _ = view(market, tmp_path)
    client = TestClient(create_app(reader))
    full = client.get('/api/v1/status').json()
    assert full['summary']['targets'] == 2 and full['summary']['selected'] == 2
    missing = client.get('/api/v1/status?health=DATA_MISSING').json()
    assert [row['data_health'] for row in missing['targets']] == ['DATA_MISSING']
    assert missing['summary']['selected'] == 1
    prefixed = client.get('/api/v1/status?symbol=bt').json()
    assert [row['asset'] for row in prefixed['targets']] == ['BTC', 'BTC']
    blocked_rows = client.get('/api/v1/status?blocked=1').json()['targets']
    assert len(blocked_rows) == 1 and all(row['blockers'] for row in blocked_rows)
    assert client.get('/api/v1/status?sort=bogus').status_code == 400
    legacy = client.get('/api/status?health=DATA_MISSING').json()
    assert legacy['targets'] == missing['targets']
    sorted_rows = client.get('/api/v1/status?sort=last_bar&desc=1').json()['targets']
    assert sorted_rows[-1]['last_bar'] is None
    page = client.get('/?health=DATA_MISSING&sort=last_scan&desc=1')
    assert page.status_code == 200
    assert '应用筛选' in page.text and '显示 1 / 2 个目标' in page.text
    assert "form-action 'self'" in page.headers['content-security-policy']
    assert '查看投递明细' in page.text and '查看全部回放' in page.text


def test_notification_delivery_detail_is_visible(market, tmp_path):
    reader, monitor, _ = view(market, tmp_path)
    with monitor.store._connection() as db:
        decision = db.execute('SELECT decision_id FROM decisions LIMIT 1').fetchone()['decision_id']
        db.execute("INSERT INTO events VALUES('E1',?,'MARKET_SIGNAL','ENTRY','INFO','{}')", (decision,))
        db.execute("INSERT INTO outbox(notification_id,event_id,status,attempts,last_error) "
                   "VALUES('N1','E1','FAILED',2,'boom')")
        db.execute("INSERT INTO notification_attempts VALUES('L1','N1','chan',1,"
                   "'2026-01-01T00:00:00Z','2026-01-01T00:00:01Z','FAILED','boom',500,NULL)")
    client = TestClient(create_app(reader))
    rows = client.get('/api/v1/notifications?status=FAILED').json()
    assert rows and rows[0]['symbol'] == 'BTC' and rows[0]['attempts'] == 2
    detail = client.get('/api/v1/notifications/N1').json()
    assert detail['attempts'][0]['http_status'] == 500
    assert client.get('/api/v1/notifications?status=BOGUS').json() == []
    page = client.get('/r/notifications')
    assert page.status_code == 200 and 'FAILED' in page.text and 'boom' in page.text
    attempts_page = client.get('/r/notifications/N1')
    assert attempts_page.status_code == 200 and '投递尝试' in attempts_page.text
    assert client.get('/r/notifications/MISSING').status_code == 404


def test_replay_sessions_record_progress_and_are_visible(market, tmp_path):
    path = tmp_path / 'replay.sqlite3'
    store = ReplaySessionStore(path)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session_id = store.start('BTC', 'D1', 'ver-1', 4, now=started)
    store.advance(session_id, 2, now=started)
    store.finish(session_id, status='SUCCEEDED', compared_bars=3, mismatch_count=0,
                 uncompared_bars=1, now=started)
    reader, _monitor, _ = view(market, tmp_path, ReplaySessionReader(path))
    client = TestClient(create_app(reader))
    sessions = client.get('/api/v1/replay').json()['sessions']
    assert sessions[0]['session_id'] == session_id and sessions[0]['status'] == 'SUCCEEDED'
    assert sessions[0]['bar_index'] == 2 and sessions[0]['total_bars'] == 4
    assert client.get('/api/v1/replay/' + session_id).json()['compared_bars'] == 3
    assert client.get('/api/v1/replay/missing').status_code == 404
    page = client.get('/r/replay')
    assert page.status_code == 200 and '历史回放' in page.text and session_id[:10] in page.text
    detail = client.get('/r/replay/' + session_id)
    assert detail.status_code == 200 and '已比较' in detail.text and '进度' in detail.text
    assert client.get('/').text.count('回放任务') == 1


def test_replay_session_store_rejects_invalid_input(tmp_path):
    store = ReplaySessionStore(tmp_path / 'replay.sqlite3')
    try:
        store.start('BTC', 'D1', 'ver-1', 0)
        raise AssertionError('replay total bars must be positive')
    except ValueError:
        pass
    try:
        store.start('BTC', 'D1', '', 3)
        raise AssertionError('replay dataset version is required')
    except ValueError:
        pass
    session_id = store.start('BTC', 'D1', 'ver-1', 3)
    try:
        store.finish(session_id, status='RUNNING')
        raise AssertionError('finish status must be terminal')
    except ValueError:
        pass
    assert ReplaySessionReader(tmp_path / 'missing.sqlite3').list() == []


def test_empty_dashboard_renders_filters_and_empty_tables(tmp_path):
    store = EventStore(tmp_path / 'events.sqlite3')
    config = RuntimeConfig(runtime_dir=tmp_path, refresh=False)
    reader = DashboardReader(store, config, None, repository_error='boom')
    client = TestClient(create_app(reader))
    page = client.get('/')
    assert page.status_code == 200
    assert '显示 0 / 0 个目标' in page.text and '暂无回放会话' in page.text
    assert client.get('/r/replay').status_code == 200
    notifications = client.get('/r/notifications')
    assert notifications.status_code == 200 and '当前筛选没有记录' in notifications.text
    assert client.get('/r/', follow_redirects=False).status_code == 307
    assert client.get('/api/v1/replay').json() == {'sessions': []}
