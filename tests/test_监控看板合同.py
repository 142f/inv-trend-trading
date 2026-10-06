"""HTTP views must show recorded evidence without reactivating blocked signals."""
from datetime import timedelta
import hashlib
import json

from fastapi.testclient import TestClient

from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.运行配置 import RuntimeConfig
from inv_trend.monitoring.看板 import DashboardReader, create_app
from inv_trend.monitoring.策略适配 import ReferenceAccount
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_数据封版合同 import service as service, change_report


def view(market, tmp_path):
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
    reader = DashboardReader(monitor.store, config, repository, clock=lambda: clock[0])
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
    assert hashlib.sha256(monitor.store.path.read_bytes()).hexdigest() == before
    assert client.post('/api/status').status_code == 405
    assert client.get('/api/targets/UNKNOWN/D1').status_code == 404
    assert client.get('/api/targets/BTC/H1').status_code == 404


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
