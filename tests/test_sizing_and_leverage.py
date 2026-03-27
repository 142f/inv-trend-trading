from __future__ import annotations

import pandas as pd

from config.models import Settings
from risk.dynamic_sizing import apply_dynamic_risk_pct
from risk.sizing import build_size_plan


def test_size_plan_respects_hard_leverage():
    settings = Settings()
    plan = build_size_plan(
        settings=settings,
        side="long",
        grade="A",
        equity=1000.0,
        entry_price=100.0,
        stop_price=99.5,
        atr=1.0,
    )
    assert plan.position_size > 0
    assert plan.applied_leverage <= settings.risk.max_leverage_hard


def test_dynamic_risk_pct_is_bounded():
    settings = Settings()
    idx = pd.date_range("2025-01-01", periods=320, freq="15min", tz="UTC")
    close = pd.Series([3000.0 + i * 0.8 for i in range(len(idx))], index=idx)
    df = pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
            "volume": 1000.0,
            "atr14": 12.0,
            "close_ts": idx + pd.Timedelta(minutes=15),
        },
        index=idx,
    )
    out = apply_dynamic_risk_pct(
        settings=settings,
        base_risk_pct=0.004,
        side="long",
        fr_15m=df,
        decision_close_ts=df["close_ts"].iloc[-1],
    )
    assert settings.risk.risk_pct_min <= out <= settings.risk.risk_pct_max
