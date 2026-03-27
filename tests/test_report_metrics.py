from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from backtest.report import build_report
from execution.fills import Fill


def _ts(i: int) -> datetime:
    return datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)


def test_report_uses_trade_lifecycle_pnl_not_fee_only():
    fills = [
        # Trade 1 (long): net positive
        Fill(_ts(0), "BTCUSDT", "long", "open", 100.0, 1.0, 100.0, 0.10, 0.0, "entry"),
        Fill(_ts(1), "BTCUSDT", "long", "reduce", 110.0, 0.5, 55.0, 0.055, 0.0, "tp1"),
        Fill(_ts(2), "BTCUSDT", "long", "close", 120.0, 0.5, 60.0, 0.06, 0.0, "close"),
        # Trade 2 (short): net negative
        Fill(_ts(3), "ETHUSDT", "short", "open", 200.0, 1.0, 200.0, 0.20, 0.0, "entry"),
        Fill(_ts(4), "ETHUSDT", "short", "close", 210.0, 1.0, 210.0, 0.21, 0.0, "stop"),
    ]

    # Net per trade:
    # T1 gross = (110-100)*0.5 + (120-100)*0.5 = 15
    # T1 fee = 0.10 + 0.055 + 0.06 = 0.215 => 14.785
    # T2 gross = (200-210)*1 = -10
    # T2 fee = 0.20 + 0.21 = 0.41 => -10.41
    # Avg = (14.785 - 10.41)/2 = 2.1875 ; win_rate=0.5
    equity_curve = pd.Series(
        [10000.0, 10004.375],
        index=pd.DatetimeIndex([_ts(0), _ts(4)], tz="UTC"),
        name="equity",
    )

    report = build_report(equity_curve=equity_curve, fills=fills, initial_equity=10000.0)
    assert report.trade_count == 2
    assert abs(report.win_rate - 0.5) < 1e-9
    assert abs(report.avg_pnl_per_trade - 2.1875) < 1e-9
