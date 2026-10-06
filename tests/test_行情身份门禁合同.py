from dataclasses import replace

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.config import load_instruments
from inv_trend.data.models import ProviderIdentityError
from inv_trend.data.providers import CsvBarsProvider
from inv_trend.storage.结构化存储_v3 import UnifiedStore


def ingest_csv(tmp_path, claims):
    instrument = replace(load_instruments()['BTC'], primary_source='licensed_csv', fallback_sources=())
    frame = pd.DataFrame({'timestamp': pd.date_range('2026-09-01', periods=3, tz='UTC'),
                          'open': [100.,101.,102.], 'high': [102.,103.,104.],
                          'low': [99.,100.,101.], 'close': [101.,102.,103.], 'volume': [10.,10.,10.]})
    for key, value in claims.items():
        frame[key] = value
    source = tmp_path/'输入.csv'
    frame.to_csv(source,index=False)
    root = tmp_path/'数据'
    UnifiedStore(root,create=True)
    service = HistoricalDataService(root,instruments={'BTC':instrument},
                                    providers={'licensed_csv':CsvBarsProvider(source)})
    return service, lambda: service.ingest('BTC','D1',pd.Timestamp('2026-09-01T00:00Z').to_pydatetime(),
                                           pd.Timestamp('2026-09-04T00:00Z').to_pydatetime(),retries=1)


@pytest.mark.parametrize('claims', [
    {'source_symbol':'ETHUSDT'}, {'timeframe':'H4'}, {'symbol':'ETH'},
    {'instrument_id':'ETHUSDT.BINANCE.SPOT'}, {'market':'xnas'}, {'asset_class':'equity'},
    {'source_symbol':['BTCUSDT','ETHUSDT','BTCUSDT']}, {'timeframe':['D1',None,'D1']},
])
def test_explicit_conflict_cannot_publish(tmp_path,claims):
    service, ingest = ingest_csv(tmp_path,claims)
    with pytest.raises(ProviderIdentityError):
        ingest()
    assert service.lake.current_version('BTC','D1') is None
    assert service.verify_catalog()['valid_current_records'] == 0


def test_matching_explicit_identity_and_timeframe_alias_can_publish(tmp_path):
    service, ingest = ingest_csv(tmp_path,{'source_symbol':'BTCUSDT','symbol':'BTC',
                                         'instrument_id':'BTCUSDT.BINANCE.SPOT',
                                         'market':'binance_spot','asset_class':'crypto','timeframe':'1D'})
    manifest = ingest()
    assert manifest.quality_status == 'CURATED'
    assert len(service.load_bars('BTC','D1')) == 3
