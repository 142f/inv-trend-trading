"""美股日线标签、4H收盘、夏令时和已核验提前收盘合同。"""
from dataclasses import replace

import pandas as pd
import pytest

from inv_trend.adapters.detector.freshness import FreshnessPolicy
from inv_trend.data.calendar import classify_missing
from inv_trend.data.config import load_instruments
from inv_trend.data.processing import assess_quality, normalize_bars
from inv_trend.monitoring.新鲜度 import MonitoringFreshnessPolicy


def instrument():
    return load_instruments()['MSFT']


def bars(stamps):
    return pd.DataFrame({'timestamp': pd.to_datetime(stamps, utc=True), 'open': 10.,
                         'high': 11., 'low': 9., 'close': 10., 'volume': 100., 'is_complete': True})


def test_daily_native_open_labels_are_not_manufactured_gaps():
    stamps = pd.date_range('2026-01-05T14:30Z', periods=3, freq='D')
    absent, breakdown = classify_missing(stamps, market='xnas', session='regular',
                                         start=stamps[0], end=stamps[-1], freq='1D')
    assert absent.empty and breakdown['provider_gap'] == 0


@pytest.mark.parametrize('frame,start,end', [
    ('D1', '2026-01-05T14:30Z', '2026-01-05T21:00Z'),
    ('D1', '2026-03-09T13:30Z', '2026-03-09T20:00Z'),
    ('D1', '2026-11-27T14:30Z', '2026-11-27T18:00Z'),
    ('H4', '2026-01-05T18:30Z', '2026-01-05T21:00Z'),
    ('H4', '2026-03-09T17:30Z', '2026-03-09T20:00Z'),
    ('H4', '2026-11-27T14:30Z', '2026-11-27T18:00Z'),
])
def test_bar_is_complete_at_session_end_and_never_one_second_earlier(frame, start, end):
    clock = pd.Timestamp(end)
    before = normalize_bars(bars([start]), instrument(), frame, 'fixture',
                            now=(clock - pd.Timedelta(seconds=1)).to_pydatetime()).clean
    after = normalize_bars(bars([start]), instrument(), frame, 'fixture', now=clock.to_pydatetime()).clean
    assert before['is_complete'].tolist() == [False]
    assert after['is_complete'].tolist() == [True]
    assert after['bar_end'].tolist() == [clock]


def test_h4_off_grid_label_is_quarantined():
    result = normalize_bars(bars(['2026-01-05T16:00Z']), instrument(), 'H4', 'fixture',
                            now=pd.Timestamp('2026-01-06T00:00Z').to_pydatetime())
    assert result.clean.empty
    assert result.quarantine['quarantine_reason'].str.contains('invalid_session_bar').all()


def test_daily_holiday_record_is_quarantined_even_if_provider_claims_complete():
    result = normalize_bars(bars(['2026-01-01T00:00Z']), instrument(), 'D1', 'fixture',
                            now=pd.Timestamp('2026-01-03T00:00Z').to_pydatetime())
    assert result.clean.empty
    assert result.quarantine['quarantine_reason'].str.contains('invalid_session_bar').all()


@pytest.mark.parametrize('missing', [False, True])
def test_equity_h4_quality_counts_two_session_bars_and_detects_missing_last_bar(missing):
    stamps = ['2026-01-05T14:30Z', '2026-01-05T18:30Z',
              '2026-01-06T14:30Z', '2026-01-06T18:30Z']
    raw = bars(stamps[:-1] if missing else stamps)
    normalized = normalize_bars(raw, instrument(), 'H4', 'fixture',
                                now=pd.Timestamp('2026-01-07T00:00Z').to_pydatetime())
    report = assess_quality(normalized, instrument(), 'H4',
                            requested_start=pd.Timestamp('2026-01-05T00:00Z'),
                            requested_end=pd.Timestamp('2026-01-06T21:00Z'))
    assert report.theoretical_bars == 4
    assert report.missing_count == int(missing)
    assert report.backtest_suitable is (not missing)


@pytest.mark.parametrize('market', ['xnas', 'xnys'])
def test_daily_freshness_honors_verified_early_close(market):
    item = replace(instrument(), market=market)
    result = FreshnessPolicy().evaluate(item, pd.Timestamp('2026-11-27T14:30Z'),
                                       now=pd.Timestamp('2026-11-27T18:16Z').to_pydatetime())
    assert result.expected_date == '2026-11-27' and result.is_fresh


@pytest.mark.parametrize('now,latest', [
    ('2026-01-05T18:29:59Z', '2026-01-02T18:30Z'),
    ('2026-01-05T18:31:01Z', '2026-01-05T14:30Z'),
    ('2026-01-05T21:01:01Z', '2026-01-05T18:30Z'),
    ('2026-03-09T20:01:01Z', '2026-03-09T17:30Z'),
    ('2026-11-27T18:01:01Z', '2026-11-27T14:30Z'),
    ('2026-07-03T20:01:01Z', '2026-07-02T17:30Z'),
])
def test_h4_freshness_uses_latest_closed_session_bar(now, latest):
    result = MonitoringFreshnessPolicy().evaluate(instrument(), 'H4', pd.Timestamp(latest),
                                                 now=pd.Timestamp(now).to_pydatetime())
    assert result.is_fresh and result.expected_date == pd.Timestamp(latest).isoformat()
