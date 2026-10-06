"""真实 Parquet、SQLite/DuckDB/v3 的数据发布合同和失败路径。"""
from datetime import datetime, timezone
import json

import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.integrity import sha256_file
from inv_trend.data.models import DataLineageError
from inv_trend.data.storage import open_data_lake
from inv_trend.storage.结构化存储_v3 import UnifiedStore
from tests.test_historical_data import FakeProvider, bars


_PROCESS_ABORT = '''
import os, sys
def abort(code):
    if sys.argv[3] == 'kernel' and os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.TerminateProcess.restype = ctypes.c_int
        if not kernel.TerminateProcess(kernel.GetCurrentProcess(), code):
            raise ctypes.WinError(ctypes.get_last_error())
        raise RuntimeError('kernel termination unexpectedly returned')
    os._exit(code)
'''


@pytest.fixture(params=['sqlite', 'duckdb', 'v3'])
def service(tmp_path, request):
    if request.param == 'v3':
        UnifiedStore(tmp_path, create=True)
    else:
        (tmp_path / 'catalog_backend.json').write_text(json.dumps({
            'backend': request.param, 'catalog_path': f'catalog.{"sqlite3" if request.param == "sqlite" else "duckdb"}',
            'schema_version': 1,
        }), encoding='utf-8')
    result = HistoricalDataService(tmp_path, providers={'binance': FakeProvider(bars())})
    result.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=timezone.utc),
                  datetime(2024, 1, 4, tzinfo=timezone.utc))
    return result


def change_report(service, mutate):
    pointer = service.lake.current_version('BTC', 'D1')
    manifest_path = service.lake._resolve_root_relative(pointer['dataset_manifest_path'])
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    report_path = service.lake._resolve_root_relative(manifest['quality_report_path'])
    report = json.loads(report_path.read_text(encoding='utf-8'))
    mutate(report)
    # Deliberately install a coherently re-hashed malformed contract. Testing
    # only mismatched file bytes would mask the semantic validation weakness.
    if hasattr(service.lake, 'store'):
        from inv_trend.storage.结构化存储_v3 import canonical, digest
        store = service.lake.store
        with store.connect() as db:
            def replace_document(path, payload):
                content = canonical(payload)
                object_id = digest(content)
                db.execute('INSERT OR IGNORE INTO document_payloads VALUES(?,?,?,?,?)',
                           (object_id, content, 'identity', len(content), 'contract-test'))
                db.execute('UPDATE documents SET sha256=? WHERE logical_path=?',
                           (object_id, store.key(path)))
            replace_document(report_path, report)
            manifest['quality_report_sha256'] = digest(canonical(report))
            replace_document(manifest_path, manifest)
    else:
        report_path.write_text(json.dumps(report), encoding='utf-8')
        manifest['quality_report_sha256'] = sha256_file(report_path)
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')


@pytest.mark.parametrize('field', ['missing_intervals', 'instrument_id'])
def test_coverage_missing_contract_field_fails(service, field):
    change_report(service, lambda report: report.pop(field))
    with pytest.raises(DataLineageError):
        service.coverage('BTC', 'D1')


def test_v3_with_legacy_catalog_conflict_fails(tmp_path):
    UnifiedStore(tmp_path, create=True)
    (tmp_path / 'catalog.duckdb').write_bytes(b'conflicting authority')
    with pytest.raises(DataLineageError):
        open_data_lake(tmp_path)


@pytest.mark.parametrize('field,value', [
    ('dataset_version', 'wrong-version'), ('actual_bars', 999),
    ('instrument_id', 'ETHUSDT.BINANCE.SPOT'), ('timeframe', 'H4'),
    ('missing_intervals', None),
    ('actual_bars', 2.9), ('actual_bars', True), ('backtest_suitable', False),
])
def test_coverage_misbound_report_fails(service, field, value):
    change_report(service, lambda report: report.update({field: value}))
    with pytest.raises(DataLineageError):
        service.coverage('BTC', 'D1')


def test_sqlite_duckdb_without_marker_conflict_fails(tmp_path):
    (tmp_path / 'catalog.sqlite3').write_bytes(b'one')
    (tmp_path / 'catalog.duckdb').write_bytes(b'two')
    with pytest.raises(DataLineageError):
        open_data_lake(tmp_path)


@pytest.mark.parametrize('marker', [None, [], 'sqlite',
    {'backend': 'sqlite', 'catalog_path': 'catalog.sqlite3', 'schema_version': 2},
    {'backend': 'sqlite', 'catalog_path': 'catalog.sqlite3', 'schema_version': True},
    {'backend': 'sqlite', 'engine': 'duckdb', 'catalog_path': 'catalog.sqlite3', 'schema_version': 1},
])
def test_invalid_catalog_marker_never_selects_or_creates_a_catalog(tmp_path, marker):
    path = tmp_path / 'catalog_backend.json'
    original = json.dumps(marker).encode()
    path.write_bytes(original)
    with pytest.raises(DataLineageError):
        open_data_lake(tmp_path)
    assert path.read_bytes() == original
    assert not (tmp_path / 'catalog.sqlite3').exists()
    assert not (tmp_path / 'catalog.duckdb').exists()


def make_candidate(service):
    config = service.instruments['BTC']
    service.instruments['BTC'] = config.__class__(**{**config.__dict__, 'revision_overlap_bars': 1})
    revised = bars()
    revised.loc[0, 'close'] = 10
    service.providers['binance'] = FakeProvider(revised)
    return service.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=timezone.utc),
                          datetime(2024, 1, 4, tzinfo=timezone.utc))


def test_approved_retry_after_new_current_is_idempotent(service):
    candidate = make_candidate(service)
    version = service.approve_review(candidate.run_id, 'verified', actor='contract')
    # A deliberate audited rollback models a newer current lineage. Retrying
    # an already committed approval must return its ID without reactivation.
    parent = json.loads((service.lake.root / 'dataset_manifests' / f'{version}.json').read_text())['parent_dataset_version']
    service.lake.rollback('BTC', 'D1', parent, 'contract test', actor='contract')
    before = service.lake.current_version('BTC', 'D1')
    assert service.approve_review(candidate.run_id, 'verified', actor='contract') == version
    assert service.lake.current_version('BTC', 'D1') == before


@pytest.mark.parametrize('failure_point', ['publish_curated', '_write_review_record'])
def test_approval_failure_restores_current_and_review(service, monkeypatch, failure_point):
    candidate = make_candidate(service)
    before = service.lake.current_version('BTC', 'D1')
    owner = service.lake if failure_point == 'publish_curated' else service.review_approvals
    def fail(*args, **kwargs):
        raise RuntimeError('injected publication failure')
    with monkeypatch.context() as scoped:
        scoped.setattr(owner, failure_point, fail)
        with pytest.raises((RuntimeError, DataLineageError)):
            service.approve_review(candidate.run_id, 'verified', actor='contract')
    assert service.lake.current_version('BTC', 'D1') == before
    assert not (service.lake.root / 'reviews' / f'{candidate.run_id}.json').exists()
    assert len(service.load_bars('BTC', 'D1')) == 2
    version = service.approve_review(candidate.run_id, 'verified', actor='contract')
    assert service.lake.current_version('BTC', 'D1')['version'] == version


@pytest.mark.parametrize('termination', ['crt', 'kernel'])
def test_approval_process_crash_before_review_recovers(service, termination):
    import subprocess
    import sys
    candidate = make_candidate(service)
    before = service.lake.current_version('BTC', 'D1')
    script = _PROCESS_ABORT + '''
import os, sys
from inv_trend.data.api import HistoricalDataService
service = HistoricalDataService(sys.argv[1])
def terminate_at_commit(*args, **kwargs):
    print('commit-fault-reached', flush=True)
    abort(73)
service.review_approvals._write_review_record = terminate_at_commit
service.approve_review(sys.argv[2], 'verified', actor='contract')
'''
    process = subprocess.run([sys.executable, '-c', script, str(service.lake.root), candidate.run_id, termination],
                             capture_output=True, timeout=45)
    assert process.returncode == 73, process.stderr.decode(errors='replace')
    assert b'commit-fault-reached' in process.stdout
    restarted = HistoricalDataService(service.lake.root)
    assert restarted.lake.current_version('BTC', 'D1') == before
    assert not (restarted.lake.root / 'reviews' / f'{candidate.run_id}.json').exists()
    assert len(restarted.load_bars('BTC', 'D1')) == 2
    version = restarted.approve_review(candidate.run_id, 'verified', actor='contract')
    assert restarted.lake.current_version('BTC', 'D1')['version'] == version


@pytest.mark.parametrize('termination', ['crt', 'kernel'])
def test_approval_process_crash_after_commit_preserves_publication(service, termination):
    import subprocess
    import sys
    candidate = make_candidate(service)
    script = _PROCESS_ABORT + '''
import os, sys
from inv_trend.data.api import HistoricalDataService
service = HistoricalDataService(sys.argv[1])
activate = service.lake.activate_curated
def terminate_after_commit(*args, **kwargs):
    activate(*args, **kwargs)
    print('committed-fault-reached', flush=True)
    abort(74)
service.lake.activate_curated = terminate_after_commit
service.approve_review(sys.argv[2], 'verified', actor='contract')
'''
    process = subprocess.run([sys.executable, '-c', script, str(service.lake.root), candidate.run_id, termination],
                             capture_output=True, timeout=45)
    assert process.returncode == 74, process.stderr.decode(errors='replace')
    assert b'committed-fault-reached' in process.stdout
    restarted = HistoricalDataService(service.lake.root)
    decision = json.loads((restarted.lake.root / 'reviews' / f'{candidate.run_id}.json').read_text())
    version = decision['published_version']
    before_retry = restarted.lake.current_version('BTC', 'D1')
    assert before_retry['version'] == version
    assert restarted.review_approvals._receipt_path(candidate.run_id).exists()
    assert len(restarted.load_bars('BTC', 'D1')) == 2
    assert restarted.approve_review(candidate.run_id, 'verified', actor='contract') == version
    assert restarted.lake.current_version('BTC', 'D1') == before_retry


def test_reject_is_idempotent_and_approve_fails(service):
    candidate = make_candidate(service)
    before = service.lake.current_version('BTC', 'D1')
    first = service.lake.review(candidate.run_id, 'rejected', 'unresolved', actor='contract')
    original = first.read_bytes()
    assert service.lake.review(candidate.run_id, 'rejected', 'unresolved', actor='contract').read_bytes() == original
    with pytest.raises(DataLineageError):
        service.approve_review(candidate.run_id, 'verified', actor='contract')
    assert service.lake.current_version('BTC', 'D1') == before


def test_lock_liveness_query_does_not_terminate_owner(tmp_path):
    import subprocess
    import sys
    import os
    from inv_trend.data.locking import _owner_process_state
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    try:
        assert _owner_process_state({'pid': os.getpid()}) is True
        assert _owner_process_state({'pid': child.pid}) is True
        assert child.poll() is None
        child.communicate(timeout=10)
        assert _owner_process_state({'pid': child.pid}) is False
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def test_reopen_preserves_rejection_and_creates_pending_attempt(service):
    candidate = make_candidate(service)
    rejected = service.reject_review(candidate.run_id, 'unresolved', actor='contract')
    original = rejected.read_bytes()
    new_id = service.reopen_review(candidate.run_id, 'new evidence', actor='contract')
    assert new_id != candidate.run_id
    assert service.reopen_review(candidate.run_id, 'new evidence', actor='contract') == new_id
    assert rejected.read_bytes() == original
    version = service.approve_review(new_id, 'verified', actor='contract')
    assert service.lake.current_version('BTC', 'D1')['version'] == version
    assert len(service.load_bars('BTC', 'D1')) == 2


def test_approval_manifest_tampering_blocks_repository(service):
    candidate = make_candidate(service)
    version = service.approve_review(candidate.run_id, 'verified', actor='contract')
    # Tamper an immutable source file referenced by the approval. Database
    # document corruption is exercised separately by the report-contract cases.
    source = service.lake._resolve_root_relative(service.lake.current_version('BTC', 'D1')['path'])
    from pathlib import Path
    physical = Path(source.__fspath__())
    physical.write_bytes(b'tampered parquet')
    from inv_trend.data.review import verify_approval_manifest
    with pytest.raises(DataLineageError):
        verify_approval_manifest(service.lake, service.lake.root / 'manifests' / f'approval-{version}.json')


def test_startup_detects_current_drift_and_repair_restores(service):
    before = service.lake.current_version('BTC', 'D1')
    with service.lake._connect() as db:
        db.execute("UPDATE current_versions SET version='wrong-current' WHERE symbol='BTC' AND timeframe='D1'")
    with pytest.raises(DataLineageError):
        HistoricalDataService(service.lake.root)
    doctor = HistoricalDataService(service.lake.root, startup_check=False)
    report = doctor.lake.check_current_consistency(raise_on_error=False)
    assert not report['ok'] and len(report['issues']) == 1
    repaired = doctor.lake.repair_current('BTC', 'D1')
    assert repaired['version'] == before['version']
    restarted = HistoricalDataService(service.lake.root)
    assert restarted.lake.check_current_consistency()['ok']
    assert len(restarted.load_bars('BTC', 'D1')) == 2


def test_repair_does_not_accept_corrupt_lineage(service):
    change_report(service, lambda report: report.update({'actual_bars': 999}))
    before = service.lake._current_catalog_row('BTC', 'D1')
    with pytest.raises(DataLineageError):
        service.lake.repair_current('BTC', 'D1')
    assert service.lake._current_catalog_row('BTC', 'D1') == before


def test_real_process_lock_prevents_reentry_and_releases_after_exit(tmp_path):
    import subprocess
    import sys
    from inv_trend.data.locking import FileLock
    lock = tmp_path / 'process.lock'
    script = '''
import sys
from pathlib import Path
from inv_trend.data.locking import FileLock
with FileLock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(lock)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(TimeoutError):
            FileLock(lock, timeout=0.15).acquire()
        assert child.poll() is None
        child.communicate(timeout=10)
        with FileLock(lock, timeout=1):
            assert child.poll() == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def test_backtest_load_uses_repository_quality_gate(service):
    from inv_trend.application.backtest.models import BacktestSourceBundle, SourceInstrument
    from inv_trend.application.backtest.source import load_versioned_data
    pointer = service.lake.current_version('BTC', 'D1')
    source = BacktestSourceBundle(source_run_id='contract', source_run_path='', strategy_version='contract',
        instruments=(SourceInstrument(symbol='BTC', instrument_id=service.instrument('BTC').instrument_id,
            timeframe='D1', dataset_version=pointer['version'], data_result_hash='a'*64,
            screening_result_hash='b'*64, decision_result_hash='c'*64),), strategy_config={})
    data, lineage = load_versioned_data(source, service.lake.root)
    assert len(data['BTC']) == 2
    assert lineage['BTC']['dataset_version'] == pointer['version']
    change_report(service, lambda report: report.update({'backtest_suitable': False}))
    with pytest.raises(DataLineageError):
        load_versioned_data(source, service.lake.root)


def test_catalog_transaction_rolls_back_on_error(service):
    before = service.lake._current_catalog_row('BTC', 'D1')
    with pytest.raises(RuntimeError):
        with service.lake._connect() as db:
            db.execute("UPDATE current_versions SET version='uncommitted' WHERE symbol='BTC'")
            raise RuntimeError('transaction fault')
    assert service.lake._current_catalog_row('BTC', 'D1') == before


def test_manifest_cannot_reference_outside_root(service, tmp_path):
    outside = tmp_path.parent / 'outside.json'
    with pytest.raises((DataLineageError, RuntimeError)):
        service.lake._resolve_root_relative(str(outside))


@pytest.mark.parametrize('via_cli', [False, True])
def test_repair_demotes_legacy_compatibility_without_promoting_data(tmp_path, via_cli):
    from inv_trend.data.legacy import migrate_legacy_csv
    source = tmp_path / 'source'
    source.mkdir()
    frame = bars()
    frame['symbol'] = 'OLD'
    frame['timeframe'] = 'D1'
    frame.to_csv(source / 'old.csv', index=False)
    migrate_legacy_csv(source, tmp_path / 'legacy')
    from inv_trend.storage.数据迁移_v3 import migrate
    migrate(tmp_path / 'legacy', tmp_path / 'data')
    lake = open_data_lake(tmp_path / 'data')
    head = lake.store.rows("SELECT * FROM dataset_heads WHERE channel='legacy'")[0]
    # Model the old migration's compatibility-current contamination.
    with lake.store.connect() as db:
        db.execute('INSERT INTO current_versions VALUES(?,?,?,?,?)',
                   (head['symbol'], head['timeframe'], head['version'], head['run_id'], head['updated_at']))
    if via_cli:
        import subprocess
        import sys
        process = subprocess.run([
            sys.executable, '-m', 'inv_trend.cli.market_data', '--root', str(lake.root),
            'repair-current', '--symbol', head['symbol'], '--timeframe', head['timeframe'],
        ], capture_output=True, text=True, encoding='utf-8', timeout=45)
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
    else:
        result = lake.repair_current(head['symbol'], head['timeframe'])
    assert result['action'] == 'demoted_legacy'
    assert lake.current_version(head['symbol'], head['timeframe']) is None
    assert lake.read_legacy_bars(head['symbol'], head['timeframe']).shape[0] == 3
    assert lake.store.rows("SELECT * FROM audit_events_v3 WHERE event_type='DEMOTE_LEGACY_CURRENT'")
    assert lake.check_current_consistency()['ok']


def test_concurrent_identical_approvals_publish_one_version(service):
    from concurrent.futures import ThreadPoolExecutor
    candidate = make_candidate(service)
    def approve():
        return service.approve_review(candidate.run_id, 'verified', actor='contract')
    with ThreadPoolExecutor(max_workers=4) as workers:
        versions = list(workers.map(lambda _: approve(), range(4)))
    assert len(set(versions)) == 1
    assert service.lake.current_version('BTC', 'D1')['version'] == versions[0]
    from inv_trend.data.review import verify_approval_manifest
    verify_approval_manifest(service.lake, service.lake.root / 'manifests' / f'approval-{versions[0]}.json')
    with service.lake._connect() as db:
        count = db.execute('SELECT COUNT(*) FROM datasets WHERE run_id=?', (f'approval-{versions[0]}',)).fetchone()[0]
    assert count == 1


def test_concurrent_reject_approve_has_one_final_decision(service):
    from concurrent.futures import ThreadPoolExecutor
    candidate = make_candidate(service)
    before = service.lake.current_version('BTC', 'D1')
    def operation(approve):
        try:
            return service.approve_review(candidate.run_id, 'verified', actor='contract') if approve else service.reject_review(candidate.run_id, 'unresolved', actor='contract')
        except DataLineageError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(operation, [True, False]))
    assert sum(isinstance(result, DataLineageError) for result in results) == 1
    decision = json.loads((service.lake.root / 'reviews' / f'{candidate.run_id}.json').read_text())
    if decision['decision'] == 'rejected':
        assert service.lake.current_version('BTC', 'D1') == before
    else:
        assert decision['decision'] == 'approved'
        assert service.lake.current_version('BTC', 'D1')['version'] == decision['published_version']
        assert service.review_approvals._receipt_path(candidate.run_id).exists()
