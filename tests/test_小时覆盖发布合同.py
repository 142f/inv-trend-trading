"""小时覆盖检查必须保留精确时间，完整序列可发布，真缺口被阻断。"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from inv_trend.data.models import DataQualityError
from tests.test_historical_data import FakeProvider
from tests.test_数据封版合同 import service as service


@pytest.mark.parametrize('frame, frequency', [('H4', '4h'), ('H1', '1h')])
@pytest.mark.parametrize('missing', [False, True])
def test_intraday_coverage_distinguishes_complete_series_from_real_gap(service, frame, frequency, missing):
    stamps = pd.date_range('2024-01-01', periods=12, freq=frequency, tz='UTC')
    bars = pd.DataFrame({'timestamp': stamps, 'open': 10., 'high': 11., 'low': 9.,
                         'close': 10., 'volume': 100., 'is_complete': True})
    if missing:
        bars = bars.drop(index=4)
    service.providers['binance'] = FakeProvider(bars)
    result = service.ingest('BTC', frame, datetime(2024, 1, 1, tzinfo=timezone.utc),
                            (stamps[-1] + pd.Timedelta(frequency)).to_pydatetime())
    if missing:
        assert result.quality_passed is False
        with pytest.raises((FileNotFoundError, DataQualityError)):
            service.load_bars('BTC', frame)
    else:
        assert result.quality_passed is True
        loaded = service.load_bars('BTC', frame, adjusted=False)
        assert len(loaded) == 12
        assert loaded['timestamp'].tolist() == list(stamps)
        assert service.coverage('BTC', frame)['missing_intervals'] == []
