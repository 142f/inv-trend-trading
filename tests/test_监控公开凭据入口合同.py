"""Candidate-only monitor bootstrap and fail-closed provider boundary checks."""
import json
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from inv_trend.cli.监控运行 import build_scanner
from inv_trend.monitoring.运行配置 import RuntimeConfig
from inv_trend.monitoring.运行时 import MonitoringRuntime


@pytest.mark.parametrize('symbol', ['XAU', 'XAG'])
def test_actual_monitor_bootstrap_never_uses_env_key_and_provider_failure_blocks(monkeypatch, tmp_path, symbol):
    monkeypatch.setenv('DUKASCOPY_API_KEY', 'synthetic-monitor-contract-value')
    monkeypatch.setenv('ALPHAVANTAGE_API_KEY', 'synthetic-alpha-contract-value')
    config = RuntimeConfig(data_root=tmp_path / 'data', runtime_dir=tmp_path / 'runtime',
                           symbols=(symbol,), timeframes=('D1', 'H4'), refresh=True)
    scanner = build_scanner(config)
    assert 'alpha_vantage' not in scanner.repository.providers
    provider = scanner.repository.providers['dukascopy']
    assert provider._api_key() is None
    requests = []
    def unavailable_catalogue(url):
        params = parse_qs(urlsplit(url).query)
        assert 'key' not in params
        requests.append(params)
        raise HTTPError(url, 429, 'synthetic provider limit', {}, None)
    monkeypatch.setattr(provider, '_load_json_with_body', unavailable_catalogue)
    def forbidden_strategy(*args, **kwargs):
        pytest.fail('failed provider reached strategy evaluation')
    monkeypatch.setattr(scanner.evaluator, 'evaluate', forbidden_strategy)
    with MonitoringRuntime(scanner, config) as runtime:
        results = runtime.run_once()
        assert len(results) == 2 and all(result['status'] == 'FAILED' for result in results)
        assert len(requests) == 2
        assert all(result['snapshot']['conditions']['quality_gate'] is False for result in results)
        assert all(result['snapshot']['event']['code'] == 'PROVIDER_FAILURE' for result in results)
        assert all(task['finished_at'] is not None for task in runtime.ledger.tasks())
    assert not any(event['kind'] == 'MARKET_SIGNAL' for event in scanner.store.rows('events'))


def test_new_python_worker_inherits_public_policy_without_consuming_key(monkeypatch):
    monkeypatch.setenv('DUKASCOPY_API_KEY', 'synthetic-child-contract-value')
    code = '''
import json
from inv_trend.data.provider_factory import create_default_providers
provider = create_default_providers(credential_providers=False)['dukascopy']
assert provider._api_key() is None
print(json.dumps({'public_key_absent': True, 'provider_module': type(provider).__module__}))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout)['public_key_absent'] is True
