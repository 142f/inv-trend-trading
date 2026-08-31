from __future__ import annotations

import numpy as np
import pandas as pd

from inv_trend.application.daily.trend_decision import _structured_evidence_chain
from inv_trend.application.daily_analysis import build_instrument_report_bundle
from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.strategy.daily import (
    build_indicator_lifecycles,
    prepare_daily_analysis,
)


def _bars() -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=230, freq="D", tz="UTC")
    close = np.full(len(index), 100.0)
    close[150:160] = np.arange(110.0, 120.0)
    close[160:170] = np.arange(90.0, 80.0, -1.0)
    close[170:] = 82.0 + np.sin(np.arange(len(index) - 170) / 4.0)
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
            "volume": np.linspace(1_000.0, 1_500.0, len(index)),
            "is_complete": True,
            "dataset_version": "lifecycle-v5-test",
        },
        index=index,
    )
    frame.attrs["dataset_version"] = "lifecycle-v5-test"
    return frame


def test_turtle_lifecycle_uses_entry_exit_channels_and_groups_continuation() -> None:
    bars = _bars()
    prepared = prepare_daily_analysis(
        bars,
        DailyChecksConfig(),
        session_anchor=bars.index[0],
    )
    projection = build_indicator_lifecycles(prepared)
    episodes = [item for item in projection.episodes if item.indicator_id == "turtle_20"]

    long_episode = next(item for item in episodes if item.direction == "long")
    short_episode = next(item for item in episodes if item.direction == "short")
    assert long_episode.start_timestamp == bars.index[150].isoformat()
    assert long_episode.invalidation_timestamp == bars.index[160].isoformat()
    assert short_episode.start_timestamp == bars.index[160].isoformat()
    assert long_episode.duration_periods == 10
    assert long_episode.reinforcement_count > 0
    assert len([item for item in episodes if item.direction == "long"]) == 1
    assert long_episode.metadata["exit_period"] == 10


def test_every_current_indicator_has_explicit_direction_and_quality_stays_neutral() -> None:
    bars = _bars()
    prepared = prepare_daily_analysis(
        bars,
        DailyChecksConfig(),
        session_anchor=bars.index[0],
    )
    projection = build_indicator_lifecycles(prepared)
    analyses = {item.indicator_id: item for item in projection.analyses}

    assert {item.direction for item in projection.analyses} <= {"long", "short", "neutral"}
    assert analyses["atr_quality"].direction == "neutral"
    assert analyses["relative_volume"].direction == "neutral"
    assert analyses["sma_10_20"].decision_weight == 0.0
    assert analyses["sma_stack"].decision_weight == 2.0
    assert all(0.0 <= item.strength <= 100.0 for item in projection.analyses)


def test_macd_zero_axis_direction_is_separate_from_momentum() -> None:
    bars = _bars()
    prepared = prepare_daily_analysis(
        bars,
        DailyChecksConfig(),
        session_anchor=bars.index[0],
    )
    projection = build_indicator_lifecycles(prepared)
    macd = [item for item in projection.analyses if item.indicator_id.startswith("macd_")]

    assert macd
    for item in macd:
        values = item.current_values
        assert values["zero_axis_direction"] == item.direction
        assert values["momentum_direction"] in {"long", "short", "neutral"}


def test_evidence_confidence_is_auditable_and_conflict_forces_zero() -> None:
    analyses = (
        {
            "indicator_id": "ema_trend",
            "indicator_name": "EMA",
            "timeframe": "D1",
            "role": "directional",
            "direction": "long",
            "active": True,
            "decision_weight": 1.0,
            "strength": 80.0,
        },
        {
            "indicator_id": "adx_dmi",
            "indicator_name": "ADX/DMI",
            "timeframe": "D1",
            "role": "directional_quality",
            "direction": "short",
            "active": True,
            "decision_weight": 1.0,
            "strength": 20.0,
        },
        {
            "indicator_id": "atr_quality",
            "indicator_name": "ATR",
            "timeframe": "D1",
            "role": "quality",
            "direction": "neutral",
            "active": True,
            "decision_weight": 1.0,
            "strength": 10.0,
            "current_values": {"state": "normal"},
        },
    )
    chain, summary = _structured_evidence_chain(
        analyses, raw_direction="long", family_conflict=False
    )

    assert len(chain) == 3
    assert summary["long_support"] == 0.8
    assert summary["short_support"] == 0.2
    assert summary["quality_adjustment"] == 1.0
    assert summary["composite_confidence"] == round(100 * 1.6 / 11, 4)
    assert summary["is_probability"] is False

    _, conflict = _structured_evidence_chain(
        analyses, raw_direction="conflict", family_conflict=True
    )
    assert conflict["composite_confidence"] == 0.0
    assert conflict["long_support"] == 0.8
    assert conflict["short_support"] == 0.2


def test_report_series_includes_each_current_lifecycle_anchor_for_chart_location() -> None:
    bars = _bars()
    prepared = prepare_daily_analysis(
        bars,
        DailyChecksConfig(),
        session_anchor=bars.index[0],
    )
    projection = build_indicator_lifecycles(prepared)
    analyses = tuple(item.to_dict() for item in projection.analyses)
    bundle = build_instrument_report_bundle(
        prepared,
        symbol="TEST",
        instrument_id="TEST.SPOT",
        generated_at=bars.index[-1].isoformat(),
        chart_bars=30,
        indicator_analyses=analyses,
        indicator_signal_episodes=tuple(item.to_dict() for item in projection.episodes),
    )

    visible_times = {str(item["timestamp"]) for item in bundle.series}
    anchors = {
        str(item["first_trigger_timestamp"])
        for item in analyses
        if item.get("first_trigger_timestamp")
    }
    assert anchors <= visible_times
    assert len(bundle.series) > 30
