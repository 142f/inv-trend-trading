"""Missing publication, broken published artifact and credential-bearing errors differ."""
import json
import os

import pandas as pd
import pytest

from inv_trend.observability.脱敏 import redact
from tests.test_监控扫描与回放合同 import market as market, scanner, current_time
from tests.test_数据封版合同 import service as service


def test_absent_formal_period_is_data_missing_not_failed_certification(market, tmp_path):
    repository, _ = market
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    result = monitor.scan('BTC', 'H4', now=pd.Timestamp('2024-01-02T00:00Z'))
    assert result['snapshot']['event']['code'] == 'DATA_MISSING'
    assert result['snapshot']['context']['failure']['error_type'] == 'DataUnavailableError'
    assert result['snapshot']['conditions']['quality_gate'] is False


def test_missing_published_artifact_is_manifest_failure_not_absent_dataset(market, tmp_path):
    repository, _ = market
    now = current_time(repository)
    pointer = repository.lake.current_version('BTC', 'D1')
    path = repository.lake.root / pointer['path']
    # This file belongs to the test's newly created isolated Repository.
    os.unlink(path)
    monitor = scanner(repository, tmp_path / 'live.sqlite3')
    result = monitor.scan('BTC', 'D1', now=now)
    assert result['snapshot']['event']['code'] == 'MANIFEST_FAILURE'
    assert result['snapshot']['market'] is None
    assert all(event['kind'] != 'MARKET_SIGNAL' for event in monitor.store.rows('events'))


@pytest.mark.parametrize('text', [
    '{"api_key": "PRIVATE_PROVIDER_KEY", "error": "denied"}',
    "{'access_token': 'PRIVATE_PROVIDER_KEY', 'error': 'denied'}",
    'request headers {"Authorization": "Basic PRIVATE_PROVIDER_KEY"}',
])
def test_quoted_json_or_python_error_text_cannot_leak_credentials(text):
    assert 'PRIVATE_PROVIDER_KEY' not in redact(text)
    assert 'PRIVATE_PROVIDER_KEY' not in json.dumps(redact({'message': text}))
