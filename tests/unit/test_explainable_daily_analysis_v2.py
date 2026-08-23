from __future__ import annotations

import json
import numpy as np
import pandas as pd

from inv_trend.application.daily_analysis import (
    PreparedDailyAnalysis,
    analyze_prepared_daily_analysis,
    build_breakout_assessments,
    build_instrument_report_bundle,
    build_market_assessment,
    prepare_daily_analysis,
)
from inv_trend.application.daily_models import BreakoutAssessment
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


def test_breakout_assessment_uses_event_bar_and_keeps_entry_policy_display_only() -> None:
    index = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    base = pd.DataFrame(
        {
            "close": [99.0, 106.0, 40.0],
            "channel_high_20": [100.0, 100.0, 100.0],
            "channel_low_20": [90.0, 90.0, 90.0],
            "channel_high_55": [100.0, 100.0, 100.0],
            "channel_low_55": [90.0, 90.0, 90.0],
        },
        index=index,
    )
    prepared = PreparedDailyAnalysis(
        base=base,
        strategy=None,  # type: ignore[arg-type]
        feature_request=None,  # type: ignore[arg-type]
        dataset_version="assessment-test",
    )
    checks = {
        "sma_alignment": {"direction": "long"},
        "ema_trend": {"direction": "long"},
        "macd_summary": {"direction": "long"},
        "trend_quality": {"direction": "long", "confirmed": True},
        "volatility": {"state": "normal"},
        "volume": {"confirmed": False},
        "rating": {
            "grade": "A",
            "direction": "long",
            "score": 7.0,
            "family_votes": {"turtle": "long", "sma": "long", "ema": "long", "macd": "long"},
        },
    }
    analysis = {
        "latest_bar": {"timestamp": index[1].isoformat(), "close": 106.0},
        "status": {"state": "ready"},
        "strategy_checks": checks,
        "indicators": {"turtle_20": {"breakout_level": 100.0}},
        "signals": [
            {
                "indicator": "turtle_20",
                "event": "breakout_up",
                "direction": "long",
                "signal_time": index[1].isoformat(),
                "breakout_level": 100.0,
            }
        ],
    }

    assessments = build_breakout_assessments(
        prepared,
        analysis,
        position=1,
        signal_ids={("turtle_20", index[1].isoformat(), "long"): "signal-20"},
    )
    assessment = assessments[0]

    assert assessment.assessment_id == "signal-20"
    assert assessment.current_price == 106.0
    assert assessment.previous_state == "位于通道区间内"
    assert assessment.post_state == "位于上轨上方"
    assert assessment.trend == "上升趋势"
    assert assessment.entry_direction == "做多"
    assert any("相对成交量" in item for item in assessment.quality_notes)
    assert "40.0" not in " ".join(assessment.trend_basis)
    assert build_market_assessment(prepared, analysis).entry_direction == "做多"

    checks["rating"] = {"grade": "B", "direction": "long", "score": 5.0}
    b_grade = build_breakout_assessments(prepared, analysis, position=1)[0]
    assert b_grade.trend == "上升趋势"
    assert b_grade.entry_direction == "不入场"

    checks["rating"] = {"grade": "CONFLICT", "direction": None, "score": 0.0}
    conflict = build_breakout_assessments(prepared, analysis, position=1)[0]
    assert conflict.trend == "震荡"
    assert conflict.entry_direction == "不入场"


def test_report_series_extends_to_earliest_breakout_assessment() -> None:
    bars = _bars(320)
    prepared = prepare_daily_analysis(bars, DailyChecksConfig(), session_anchor=bars.index[0])
    result = analyze_prepared_daily_analysis(prepared)
    event_time = prepared.base.index[30].isoformat()
    assessment = BreakoutAssessment(
        assessment_id="historic-breakout",
        signal_id=None,
        timestamp=event_time,
        timeframe="D1",
        current_price=float(prepared.base.iloc[30]["close"]),
        breakout_type="向上突破",
        breakout_object="海龟 20 日唐奇安上轨",
        breakout_level=1.0,
        previous_state="位于通道区间内",
        post_state="位于上轨上方",
        trend="上升趋势",
    )
    bundle = build_instrument_report_bundle(
        prepared,
        symbol="BTC",
        instrument_id="BTCUSDT.BINANCE.SPOT",
        generated_at="2026-08-21T19:00:00+08:00",
        analysis=result,
        breakout_assessments=(assessment,),
        chart_bars=10,
    )

    assert bundle.series[0]["timestamp"] == event_time
    assert bundle.breakout_assessments == (assessment,)
