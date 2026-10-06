import json
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

from inv_trend.data.config import load_instruments
from inv_trend.data.models import DownloadRequest
from inv_trend.data.provider_factory import create_default_providers
from inv_trend.data.providers import DukascopyBarsProvider


@pytest.mark.parametrize('symbol', ['XAU', 'XAG'])
@pytest.mark.parametrize('frame', ['D1', 'H1'])
def test_public_fetch_discovery_and_bars_do_not_consume_environment_key(monkeypatch, symbol, frame):
    monkeypatch.setenv('DUKASCOPY_API_KEY', 'synthetic-credential-contract-value')
    provider = create_default_providers(credential_providers=False)['dukascopy']
    observed = []
    def fake_load(url):
        params = parse_qs(urlsplit(url).query)
        observed.append(params)
        if params['path'] == ['api/instrumentList']:
            payload = [{'symbol': symbol + 'USD', 'id': 1}]
        else:
            payload = {'data': [{'time': 1704067200000, 'open': 1, 'high': 2,
                                'low': .5, 'close': 1.5, 'volume': 10}]}
        return payload, json.dumps(payload).encode()
    monkeypatch.setattr(provider, '_load_json_with_body', fake_load)
    request = DownloadRequest(load_instruments()[symbol], frame,
        pd.Timestamp('2024-01-01', tz='UTC').to_pydatetime(),
        pd.Timestamp('2024-01-02', tz='UTC').to_pydatetime())
    result = provider.fetch(request)
    assert len(result.frame) == 1
    assert len(observed) == 2 and all('key' not in params for params in observed)


def test_public_factory_never_reads_environment_credentials(monkeypatch):
    def forbidden_getenv(*args, **kwargs):
        raise AssertionError('public provider factory consumed environment')
    monkeypatch.setattr('inv_trend.data.providers.os.getenv', forbidden_getenv)
    providers = create_default_providers(credential_providers=False)
    assert set(providers) == {'binance', 'dukascopy', 'yahoo_chart'}
    assert providers['dukascopy']._api_key() is None


def test_existing_opted_in_environment_usage_remains_supported(monkeypatch):
    monkeypatch.setenv('DUKASCOPY_API_KEY', 'synthetic-credential-contract-value')
    assert DukascopyBarsProvider()._api_key() == 'synthetic-credential-contract-value'
    assert create_default_providers()['dukascopy']._api_key() == 'synthetic-credential-contract-value'


def test_explicit_provider_key_precedes_environment_without_reading_it(monkeypatch):
    def forbidden_getenv(*args, **kwargs):
        raise AssertionError('explicit credential read environment')
    monkeypatch.setattr('inv_trend.data.providers.os.getenv', forbidden_getenv)
    provider = DukascopyBarsProvider(api_key='explicit-synthetic-value', use_environment_credentials=False)
    assert provider._api_key() == 'explicit-synthetic-value'


@pytest.mark.parametrize('value', [None, 0, 'false'])
def test_environment_policy_rejects_ambiguous_values(value):
    with pytest.raises(ValueError, match='explicit boolean'):
        DukascopyBarsProvider(use_environment_credentials=value)
