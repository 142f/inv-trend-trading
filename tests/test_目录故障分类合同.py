"""Real owned-catalog failures must not masquerade as provider failures."""
import pytest
from inv_trend.data.models import CatalogAccessError

from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_监控运行链合同 import runtime
from tests.test_数据封版合同 import service as service


def break_owned_catalog(repository):
    with repository.lake._connect() as db:
        db.execute('ALTER TABLE current_versions RENAME TO isolated_broken_current_versions')


def test_real_catalog_query_failure_is_classified_on_load(market, tmp_path):
    repository, _ = market
    now = current_time(repository)
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    break_owned_catalog(repository)
    result = monitor.scan('BTC', 'D1', now=now)
    assert result['snapshot']['reasons'] == ['CATALOG_FAILURE']
    assert result['snapshot']['market'] is None
    assert all(row['kind'] != 'MARKET_SIGNAL' for row in monitor.store.rows('events'))


def test_real_catalog_query_failure_is_classified_on_update(market, tmp_path):
    repository, _ = market
    with runtime(market, tmp_path, refresh=True) as monitor:
        break_owned_catalog(repository)
        result = monitor.run_once()[0]
        assert result['status'] == 'FAILED'
        assert result['snapshot']['reasons'] == ['CATALOG_FAILURE']
        assert result['snapshot']['market'] is None
        assert all(row['kind'] != 'MARKET_SIGNAL' for row in monitor.scanner.store.rows('events'))


def test_catalog_query_failure_cannot_be_silenced_as_missing_request_start(service):
    with service.lake._connect() as db:
        db.execute('ALTER TABLE datasets RENAME TO isolated_broken_datasets')
    with pytest.raises(CatalogAccessError, match='datasets') as failure:
        service._last_requested_start('BTC', 'D1')
    assert failure.value.__cause__ is not None


def test_dashboard_catalog_failure_is_explicit_and_read_only(market, tmp_path):
    from hashlib import sha256
    from tests.test_监控看板合同 import view
    reader, monitor, _ = view(market, tmp_path)
    break_owned_catalog(market[0])
    before = sha256(monitor.store.path.read_bytes()).hexdigest()
    row = reader.overview()['targets'][0]
    assert row['data_health'] == 'CATALOG_FAILURE'
    assert row['current_signal'] == 'BLOCKED'
    assert sha256(monitor.store.path.read_bytes()).hexdigest() == before


def test_real_catalog_transaction_rolls_back_before_typed_failure(service):
    with service.lake._connect() as db:
        db.execute('CREATE TABLE isolated_rollback_probe(value INTEGER)')
    with pytest.raises(CatalogAccessError) as failure:
        with service.lake._connect() as db:
            if hasattr(service.lake, 'store'):
                db.execute('BEGIN TRANSACTION')
            db.execute('INSERT INTO isolated_rollback_probe VALUES(7)')
            db.execute('SELECT * FROM isolated_nonexistent_table')
    assert failure.value.__cause__ is not None
    with service.lake._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM isolated_rollback_probe').fetchone()[0] == 0


def test_original_unique_constraint_contract_is_preserved(service):
    with service.lake._connect() as db:
        db.execute('CREATE TABLE isolated_unique_probe(value INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO isolated_unique_probe VALUES(7)')
    with pytest.raises(Exception) as failure:
        with service.lake._connect() as db:
            db.execute('INSERT INTO isolated_unique_probe VALUES(7)')
    assert not isinstance(failure.value, CatalogAccessError)
    import sqlite3
    if service.lake._catalog_engine == 'duckdb':
        import duckdb
        assert isinstance(failure.value, duckdb.ConstraintException)
    else:
        assert isinstance(failure.value, sqlite3.IntegrityError)


def test_real_catalog_connection_open_failure_is_typed(service, tmp_path):
    directory = tmp_path / 'isolated_directory'
    directory.mkdir()
    if hasattr(service.lake, 'store'):
        service.lake.store.path = directory
    else:
        service.lake.catalog_path = directory
    with pytest.raises(CatalogAccessError) as failure:
        with service.lake._connect():
            pytest.fail('unopenable owned catalog must not yield a connection')
    assert failure.value.__cause__ is not None


def test_external_provider_database_exception_is_not_owned_catalog_failure(market, tmp_path):
    import sqlite3
    def external_failure(*args, **kwargs):
        raise sqlite3.OperationalError('external provider cache unavailable')
    with runtime(market, tmp_path, refresh=True, updater=external_failure) as monitor:
        result = monitor.run_once()[0]
        assert result['status'] == 'FAILED'
        assert result['snapshot']['reasons'] == ['PROVIDER_FAILURE']


def test_event_store_sql_failure_is_not_caught_as_market_catalog_failure(market, tmp_path, monkeypatch):
    import sqlite3
    repository, _ = market
    now = current_time(repository)
    monitor = scanner(repository, tmp_path / 'events.sqlite3')
    def failing_commit(*args, **kwargs):
        raise sqlite3.OperationalError('owned event store unavailable')
    monkeypatch.setattr(monitor.store, 'commit', failing_commit)
    with pytest.raises(sqlite3.OperationalError, match='event store'):
        monitor.scan('BTC', 'D1', now=now)
    assert monitor.store.rows('events') == []
