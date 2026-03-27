from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from data.historical_fetcher import fetch_ohlcv_range


class _FakeClient:
    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int):
        # Return exactly one candle starting at `since`.
        return [[since, 100.0, 101.0, 99.0, 100.5, 10.0]]


def test_fetch_ohlcv_range_raises_on_incomplete_when_strict():
    client = _FakeClient()
    start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError):
        fetch_ohlcv_range(
            client=client,
            symbol="BTCUSDT",
            timeframe="1h",
            start=start,
            end=end,
            limit_per_call=1,
            max_pages=2,  # not enough to cover 3 hours
            strict_complete=True,
        )


def test_fetch_ohlcv_range_allows_incomplete_when_not_strict():
    client = _FakeClient()
    start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)
    df = fetch_ohlcv_range(
        client=client,
        symbol="BTCUSDT",
        timeframe="1h",
        start=start,
        end=end,
        limit_per_call=1,
        max_pages=2,
        strict_complete=False,
    )
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
