"""重复健康周期必须恢复，质量失败分类准确，错误输出不泄露凭据。"""
from datetime import timedelta
import json

import pytest

from inv_trend.data.lineage import _validate_report_contract
from inv_trend.data.models import DataLineageError
from inv_trend.monitoring.策略适配 import ReferenceAccount
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_数据封版合同 import service as service


def test_original_dataset_recovery_is_not_swallowed_by_scan_deduplication(market, tmp_path):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    now = current_time(repository)
    account = ReferenceAccount('paper', 100000.)
    original = monitor.scan('BTC', 'D1', now=now, account=account)
    for cycle in range(2):
        clock = now + timedelta(seconds=cycle + 1)
        monitor.record_failure(repository.instrument('BTC'), 'D1', now=clock,
                                error=DataLineageError('temporary report outage'))
        assert monitor.store.state('BTC', 'D1').details['data_health'] == 'MANIFEST_FAILURE'
        restored = monitor.scan('BTC', 'D1', now=clock, account=account)
        assert restored['created'] is True
        assert monitor.store.state('BTC', 'D1').details['data_health'] == 'FRESH'
        assert restored['snapshot']['market'] == original['snapshot']['market']
        assert monitor.scan('BTC', 'D1', now=clock, account=account)['created'] is False
    events = monitor.store.rows('events')
    assert sum(event['code'] == 'DATA_RECOVERED' for event in events) == 2
    assert sum(event['kind'] == 'MARKET_SIGNAL' for event in events) == 1


def test_known_failed_certification_remains_blocked_and_is_quality_health(market, tmp_path, monkeypatch):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    def failed_contract(*args, **kwargs):
        _validate_report_contract({'instrument_id': 'BTCUSDT.SPOT', 'row_count': 1},
                                   {'instrument_id': 'BTCUSDT.SPOT', 'actual_bars': 1,
                                    'quality_status': 'CURATED', 'backtest_suitable': False,
                                    'missing_intervals': []})
    monkeypatch.setattr(repository, 'load_bars', failed_contract)
    result = monitor.scan('BTC', 'D1', now=current_time_for_fixture())
    assert result['snapshot']['conditions']['quality_gate'] is False
    assert result['snapshot']['event']['code'] == 'QUALITY_FAILURE'


def current_time_for_fixture():
    import pandas as pd
    return pd.Timestamp('2024-01-16T00:01:01Z').to_pydatetime()


@pytest.mark.parametrize('message', [
    'provider https://example.invalid/api?api_key=PRIVATE_VALUE&token=OTHER_PRIVATE_VALUE failed',
    'provider https://username:PRIVATE_VALUE@example.invalid/secret/OTHER_PRIVATE_VALUE failed',
    'Authorization: Bearer PRIVATE_VALUE failed',
    'api_key=PRIVATE_VALUE password:OTHER_PRIVATE_VALUE failed',
])
def test_failure_snapshot_and_event_redact_credentials(market, tmp_path, message):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    monitor.record_failure(repository.instrument('BTC'), 'D1', now=current_time_for_fixture(),
                            error=DataLineageError(message))
    recorded = json.dumps(monitor.store.rows('decisions') + monitor.store.rows('events'))
    assert 'PRIVATE_VALUE' not in recorded
    assert 'REDACTED' in recorded


def test_unknown_or_malformed_quality_contract_is_still_lineage_failure():
    with pytest.raises(DataLineageError, match='assessment is missing'):
        _validate_report_contract({'instrument_id': 'BTC', 'row_count': 1},
                                   {'instrument_id': 'BTC', 'actual_bars': 1, 'missing_intervals': [],
                                    'quality_status': 'CURATED'})
