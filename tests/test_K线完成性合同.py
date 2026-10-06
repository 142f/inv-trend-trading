"""来源完成性标记不能让未知或尚未收盘的 K 线成为正式数据。"""
from datetime import datetime, timezone

import pytest

from inv_trend.data.config import load_instruments
from inv_trend.data.processing import normalize_bars
from tests.test_historical_data import bars


UTC = timezone.utc


@pytest.mark.parametrize('flag', ['False', 'false', 'unknown', 'yes', 2, float('inf')])
def test_unknown_or_false_completion_marker_never_becomes_complete(flag):
    frame = bars().iloc[:1].copy()
    frame['is_complete'] = flag
    result = normalize_bars(frame, load_instruments()['BTC'], 'D1', 'binance',
                            now=datetime(2024, 1, 4, tzinfo=UTC))
    assert not result.clean['is_complete'].any()


@pytest.mark.parametrize('timeframe', ['D1', 'H4', 'H1'])
def test_provider_complete_true_cannot_override_bar_close(timeframe):
    frame = bars().iloc[:1].copy()
    frame['is_complete'] = True
    result = normalize_bars(frame, load_instruments()['BTC'], timeframe, 'binance',
                            now=datetime(2024, 1, 1, 0, 30, tzinfo=UTC))
    assert not result.clean['is_complete'].any()


@pytest.mark.parametrize('flag', [True, 1, 'true'])
def test_explicit_completed_marker_after_close_remains_complete(flag):
    frame = bars().iloc[:1].copy()
    frame['is_complete'] = flag
    result = normalize_bars(frame, load_instruments()['BTC'], 'D1', 'binance',
                            now=datetime(2024, 1, 2, tzinfo=UTC))
    assert result.clean['is_complete'].all()
