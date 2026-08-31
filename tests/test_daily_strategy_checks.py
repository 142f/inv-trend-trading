from __future__ import annotations

import pandas as pd
import pytest

from inv_trend.adapters.detector.indicators.daily_strategy_checks import (
    analyze_prepared_strategy_checks,
    prepare_daily_strategy_checks,
)
from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.math_utils import directional_movement_index
from inv_trend.core.decision_events import ExecutionDecisionEvent
from inv_trend.core.resampling import aggregate_completed_sessions
from inv_trend.core.signals import SignalEvent
from inv_trend.adapters.detector.storage.daily_signal_repository import (
    SQLiteDailySignalRepository,
)


def _bars(closes: list[float], *, volume: float = 1_000.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="D", tz="UTC")
    values = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": values,
            "high": values + 1.0,
            "low": values - 1.0,
            "close": values,
            "volume": volume,
        },
        index=index,
    )


def test_completed_session_aggregation_preserves_ohlcv_and_drops_partial() -> None:
    bars = _bars([float(value) for value in range(10, 17)])
    aggregated = aggregate_completed_sessions(bars, 3, anchor=bars.index[0])

    assert len(aggregated) == 2
    assert aggregated.index.tolist() == [bars.index[2], bars.index[5]]
    assert aggregated.iloc[0]["open"] == 10.0
    assert aggregated.iloc[0]["high"] == 13.0
    assert aggregated.iloc[0]["low"] == 9.0
    assert aggregated.iloc[0]["close"] == 12.0
    assert aggregated.iloc[0]["volume"] == 3_000.0
    assert aggregated.iloc[0]["session_count"] == 3


def test_sma_and_ema_are_independent_and_ema_waits_for_its_long_period() -> None:
    bars = _bars([100.0 + value for value in range(130)])
    prepared = prepare_daily_strategy_checks(
        bars, DailyChecksConfig(), session_anchor=bars.index[0]
    )
    result = analyze_prepared_strategy_checks(prepared)

    assert result["sma_alignment"]["status"] == "ready"
    assert result["sma_alignment"]["direction"] == "long"
    assert result["ema_trend"]["status"] == "unavailable"


def test_higher_timeframe_macd_is_not_visible_until_its_completed_bar_closes() -> None:
    closes = [100.0] * 395 + [102.0, 104.0, 106.0, 108.0, 110.0, 111.0]
    bars = _bars(closes)
    prepared = prepare_daily_strategy_checks(
        bars, DailyChecksConfig(), session_anchor=bars.index[0]
    )

    before_close = analyze_prepared_strategy_checks(prepared, position=398)
    at_close = analyze_prepared_strategy_checks(prepared, position=399)
    after_close = analyze_prepared_strategy_checks(prepared, position=400)

    assert before_close["macd"]["D5"]["bar_end"] == bars.index[394].isoformat()
    assert at_close["macd"]["D5"]["bar_end"] == bars.index[399].isoformat()
    assert {
        "indicator": "macd_12_26_9",
        "event": "golden_cross",
        "direction": "long",
        "timeframe": "D5",
        "signal_time": bars.index[399].isoformat(),
        "reference_value": at_close["macd"]["D5"]["dea"],
        "parameters": {
            "fast": 12,
            "slow": 26,
            "signal": 9,
            "adjust": False,
            "sessions_per_bar": 5,
        },
    } in at_close["signals"]
    assert not any(
        signal.get("timeframe") == "D5" for signal in after_close["signals"]
    )


def test_three_parallel_families_can_reach_a_without_turtle() -> None:
    bars = _bars([100.0 + value for value in range(420)])
    prepared = prepare_daily_strategy_checks(
        bars, DailyChecksConfig(), session_anchor=bars.index[0]
    )
    result = analyze_prepared_strategy_checks(prepared)

    assert result["rating"]["family_votes"]["turtle"] == "none"
    assert result["rating"]["aligned_families"] == ["sma", "ema", "macd"]
    assert result["rating"]["grade"] == "A"
    assert result["rating"]["direction"] == "long"


def test_wilder_dmi_adx_identifies_a_persistent_uptrend() -> None:
    bars = _bars([100.0 + value for value in range(80)])
    values = directional_movement_index(bars["high"], bars["low"], bars["close"], 14)

    assert values["plus_di"].iloc[-1] > values["minus_di"].iloc[-1]
    assert values["adx"].iloc[-1] > 25.0


def test_daily_check_configuration_rejects_unknown_nested_keys() -> None:
    with pytest.raises(ValueError, match="unsupported daily_checks.macd keys"):
        DailyChecksConfig.from_mapping({"macd": {"fast": 12, "unknown": 1}})


def test_only_execution_decision_is_enqueued_while_all_events_are_persisted(
    tmp_path,
) -> None:
    repository = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    repository.start_run("run-1", "2024-01-02T00:00:00+00:00")
    base = {
        "instrument_id": "AAA.TEST", "symbol": "AAA", "timeframe": "D1",
        "direction": "LONG", "signal_time": "2024-01-01T00:00:00+00:00",
        "detected_at": "2024-01-02T00:00:00+00:00", "trigger_price": 100.0,
        "reference_value": 1.0, "dataset_version": "dataset-1",
        "indicator_name": "test", "indicator_parameters": {},
    }
    underlying = SignalEvent.create(signal_type="SMA_STACK_BULLISH", **base)
    grade = SignalEvent.create(signal_type="STRATEGY_GRADE_A_LONG", **base)
    entry = ExecutionDecisionEvent.create(
        instrument_id="AAA.TEST",
        symbol="AAA",
        timeframe="D1",
        as_of="2024-01-01T00:00:00+00:00",
        action="ENTER_LONG",
        decision_hash="decision-1",
        strategy_version="corrected-v2",
        dataset_version="dataset-1",
        detected_at="2024-01-02T00:00:00+00:00",
        trigger_price=100.0,
        trigger_signal_ids=(grade.signal_id,),
    ).to_signal_event()

    inserted, duplicates = repository.commit_events_and_cursor(
        [underlying, grade, entry],
        run_id="run-1",
        instrument_id="AAA.TEST",
        timeframe="D1",
        strategy_version="corrected-v2",
        last_signal_time="2024-01-01T00:00:00+00:00",
        notification_signal_ids={entry.signal_id},
    )

    assert {event.signal_id for event in inserted} == {
        underlying.signal_id,
        grade.signal_id,
        entry.signal_id,
    }
    assert duplicates == 0
    assert repository.signal_count() == 3
    assert [event.signal_id for event in repository.pending_notifications("run-1")] == [
        entry.signal_id
    ]
    assert repository.get_or_create_session_anchor(
        "AAA.TEST", "D1", "2024-01-01T00:00:00+00:00"
    ) == "2024-01-01T00:00:00+00:00"
    assert repository.get_or_create_session_anchor(
        "AAA.TEST", "D1", "2020-01-01T00:00:00+00:00"
    ) == "2024-01-01T00:00:00+00:00"
