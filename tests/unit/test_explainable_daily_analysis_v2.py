from __future__ import annotations

import json
import numpy as np
import pandas as pd

from inv_trend.application.daily_analysis import (
    analyze_prepared_daily_analysis,
    build_instrument_report_bundle,
    prepare_daily_analysis,
)
from inv_trend.application.strategy_config import DailyChecksConfig


def _bars(n: int = 320) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    x = np.arange(n, dtype=float)
    close = 30000 + 35 * x + 1400 * np.sin(x / 15) + 450 * np.sin(x / 4)
    op = close * (1 + 0.002 * np.sin(x / 6))
    frame = pd.DataFrame(
        {
            "open": op,
            "high": np.maximum(op, close) + 260,
            "low": np.minimum(op, close) - 250,
            "close": close,
            "volume": 900 + 120 * (1 + np.sin(x / 10)),
            "is_complete": True,
            "dataset_version": "explain-test-v2",
        },
        index=idx,
    )
    frame.attrs["dataset_version"] = "explain-test-v2"
    return frame


def test_all_rule_children_include_required_audit_fields() -> None:
    bars = _bars()
    prepared = prepare_daily_analysis(bars, DailyChecksConfig(), session_anchor=bars.index[0])
    result = analyze_prepared_daily_analysis(prepared)
    rules = result["rule_evaluations"]
    assert rules
    required = {
        "condition_id",
        "name",
        "actual_value",
        "reference_value",
        "operator",
        "passed",
        "relative_position",
        "impact",
        "weight",
        "timestamp",
        "price",
        "status",
    }
    conditions = [condition for rule in rules for condition in rule["conditions"]]
    assert conditions
    assert all(required.issubset(condition) for condition in conditions)
    assert any(condition["passed"] is True for condition in conditions)
    assert any(condition["passed"] is False for condition in conditions)


def test_report_bundle_hash_is_stable_and_json_safe() -> None:
    bars = _bars()
    prepared = prepare_daily_analysis(bars, DailyChecksConfig(), session_anchor=bars.index[0])
    result = analyze_prepared_daily_analysis(prepared)
    kwargs = dict(
        symbol="BTC",
        instrument_id="BTCUSDT.BINANCE.SPOT",
        generated_at="2026-08-21T19:00:00+08:00",
        analysis=result,
        chart_bars=180,
    )
    first = build_instrument_report_bundle(prepared, **kwargs)
    second = build_instrument_report_bundle(prepared, **kwargs)
    assert first.result_hash == second.result_hash
    assert first.result_id == first.result_hash[:24]
    encoded = json.dumps(first.to_dict(), ensure_ascii=False, allow_nan=False)
    assert "rule_evaluations" in encoded
    assert "change_log" in encoded
