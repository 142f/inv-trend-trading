from __future__ import annotations

import numpy as np
import pandas as pd

from inv_trend.adapters.multi_asset.us_trend_alerts import (
    SIGNAL_BREAK_20_HIGH,
    SIGNAL_BREAK_20_LOW,
    SIGNAL_BREAK_55_HIGH,
    SIGNAL_BREAK_55_LOW,
    detect_trend_breakouts,
)


def _bars(close: np.ndarray) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=len(close), tz="UTC")
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": "TEST",
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000,
        }
    )


def test_detects_high_breakouts_against_shifted_channels() -> None:
    close = np.linspace(100.0, 154.0, 60)
    bars = _bars(close)
    bars.loc[59, "close"] = 170.0
    bars.loc[59, "high"] = 171.0

    alerts = detect_trend_breakouts(bars)
    signals = {alert["signal_type"] for alert in alerts}

    assert SIGNAL_BREAK_20_HIGH in signals
    assert SIGNAL_BREAK_55_HIGH in signals
    high_55 = next(alert for alert in alerts if alert["signal_type"] == SIGNAL_BREAK_55_HIGH)["high_55"]
    assert high_55 == round(float(bars["high"].iloc[4:59].max()), 4)


def test_detects_low_breakouts_against_shifted_channels() -> None:
    close = np.linspace(160.0, 106.0, 60)
    bars = _bars(close)
    bars.loc[59, "close"] = 80.0
    bars.loc[59, "low"] = 79.0

    alerts = detect_trend_breakouts(bars)
    signals = {alert["signal_type"] for alert in alerts}

    assert SIGNAL_BREAK_20_LOW in signals
    assert SIGNAL_BREAK_55_LOW in signals
    low_20 = next(alert for alert in alerts if alert["signal_type"] == SIGNAL_BREAK_20_LOW)["low_20"]
    assert low_20 == round(float(bars["low"].iloc[39:59].min()), 4)
