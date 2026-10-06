"""Failed certification is inspectable metadata, never a strategy-visible bypass."""
import pytest

from inv_trend.data.models import DataLineageError, DataUnavailableError
from tests.test_监控扫描与回放合同 import market as market
from tests.test_数据封版合同 import service as service, change_report


def test_diagnostics_return_bound_metadata_without_reading_bars(market, monkeypatch):
    repository, version = market
    monkeypatch.setattr(repository.lake, 'read_bars', lambda *a, **k: pytest.fail('diagnostic must not load bars'))
    result = repository.diagnose_current('BTC', 'D1')
    assert result['diagnostic_only'] and result['certified']
    assert result['dataset_version'] == version
    assert result['quality_report']['instrument_id'] == repository.instrument('BTC').instrument_id


def test_known_rejected_quality_is_visible_but_formal_repository_stays_blocked(market):
    repository, _ = market
    change_report(repository, lambda report: report.update(backtest_suitable=False))
    result = repository.diagnose_current('BTC', 'D1')
    assert not result['certified'] and result['quality_report']['backtest_suitable'] is False
    with pytest.raises(DataLineageError):
        repository.load_bars('BTC', 'D1')


def test_diagnostic_rehashed_misbound_report_and_missing_dataset_fail_closed(market):
    repository, _ = market
    change_report(repository, lambda report: report.update(actual_bars=999))
    with pytest.raises(DataLineageError):
        repository.diagnose_current('BTC', 'D1')
    with pytest.raises(DataUnavailableError):
        repository.diagnose_current('BTC', 'H4')
