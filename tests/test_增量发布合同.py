"""增量发布必须验证同一个基线，不能覆盖新版本或洗掉研究属性。"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.models import DataLineageError, DataQualityError
from tests.test_historical_data import FakeProvider
from tests.test_数据封版合同 import change_report, service as service


UTC = timezone.utc


def complete_bars(count=3):
    values = pd.Series(range(10, 10 + count), dtype=float)
    return pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=count, tz='UTC'),
        'open': values, 'high': values + 2, 'low': values - 1,
        'close': values + 1, 'volume': 100, 'is_complete': True,
    })


@pytest.mark.parametrize('damage', ['coverage_contract', 'report_identity'])
def test_update_never_recertifies_an_unverifiable_base(service, damage):
    old = service.lake.current_version('BTC', 'D1')
    if damage == 'coverage_contract':
        change_report(service, lambda report: report.pop('missing_intervals'))
    else:
        change_report(service, lambda report: report.update(instrument_id='foreign-instrument'))
    service.providers['binance'] = FakeProvider(complete_bars())
    with pytest.raises(DataLineageError):
        service.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=UTC),
                       datetime(2024, 1, 4, tzinfo=UTC))
    assert service.lake.current_version('BTC', 'D1') == old


def test_update_without_missing_bars_still_verifies_base(service):
    change_report(service, lambda report: report.pop('missing_intervals'))
    with pytest.raises(DataLineageError):
        service.update('BTC', 'D1', end=datetime(2024, 1, 2, tzinfo=UTC))


def test_update_cannot_claim_a_version_it_did_not_merge(service, monkeypatch):
    original_merge = service._merge_current
    winner = HistoricalDataService(service.lake.root,
                                   providers={'binance': FakeProvider(complete_bars())})
    winner.providers['binance'].frame.loc[2, 'close'] = 13.5
    winner_pointer = {}

    def merge_then_publish(*args, **kwargs):
        combined = original_merge(*args, **kwargs)
        winner.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=UTC),
                      datetime(2024, 1, 4, tzinfo=UTC))
        winner_pointer.update(winner.lake.current_version('BTC', 'D1'))
        return combined

    monkeypatch.setattr(service, '_merge_current', merge_then_publish)
    service.providers['binance'] = FakeProvider(complete_bars())
    with pytest.raises(DataLineageError):
        service.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=UTC),
                       datetime(2024, 1, 4, tzinfo=UTC))
    assert service.lake.current_version('BTC', 'D1') == winner_pointer
    assert service.load_bars('BTC', 'D1', adjusted=False).iloc[-1]['close'] == 13.5


def test_integrity_verified_base_can_be_reassessed_without_lowering_read_gate(service):
    change_report(service, lambda report: report.update(backtest_suitable=False))
    with pytest.raises(DataLineageError):
        service.load_bars('BTC', 'D1')
    service.providers['binance'] = FakeProvider(complete_bars())
    result = service.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=UTC),
                            datetime(2024, 1, 4, tzinfo=UTC))
    assert result.quality_passed is True
    assert len(service.load_bars('BTC', 'D1')) == 3


def test_incremental_update_preserves_research_only_provenance(service):
    class ResearchProvider(FakeProvider):
        def fetch(self, request):
            result = super().fetch(request)
            result.metadata['research_only'] = True
            return result

    service.providers['binance'] = ResearchProvider(complete_bars())
    first = service.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=UTC),
                           datetime(2024, 1, 4, tzinfo=UTC))
    assert first.quality_status == 'RESEARCH_ONLY'
    service.providers['binance'] = FakeProvider(complete_bars(4))
    second = service.ingest('BTC', 'D1', datetime(2024, 1, 1, tzinfo=UTC),
                            datetime(2024, 1, 5, tzinfo=UTC))
    assert second.quality_status == 'RESEARCH_ONLY'
    with pytest.raises(DataQualityError):
        service.load_bars('BTC', 'D1')
    assert len(service.load_bars('BTC', 'D1', allow_research=True)) == 4
