from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from inv_trend.adapters.detector.storage.daily_signal_repository import (
    SQLiteDailySignalRepository,
)
from inv_trend.application.daily.signal_commit import (
    _resolve_projection_notification_ids,
)
from inv_trend.application.daily.trend_decision import (
    TrendDecisionProjectionBuilder,
    TrendDecisionService,
    build_execution_decision_event,
)
from inv_trend.application.daily_models import (
    DataUpdateResult,
    StrategyScreeningResult,
    TrendDecisionResult,
)
from inv_trend.core.decision_events import ExecutionDecisionEvent
from inv_trend.core.signals import SignalEvent


AS_OF = "2026-08-22T00:00:00+00:00"
DETECTED_AT = "2026-08-23T00:00:00+00:00"


def _data() -> DataUpdateResult:
    return DataUpdateResult(
        symbol="BTC",
        instrument_id="BTC",
        timeframe="D1",
        update_status="unchanged",
        dataset_version="dataset-v1",
        latest_complete_d1=AS_OF,
        data_readiness="READY",
    )


def _rating() -> dict[str, object]:
    return {
        "grade": "A",
        "direction": "long",
        "score": 6.0,
        "family_votes": {"sma": "long", "ema": "long", "macd": "long"},
        "aligned_families": ["sma", "ema", "macd"],
    }


def _eligibility(status: str) -> dict[str, object]:
    if status == "PASSED":
        return {
            "status": "PASSED",
            "evaluated": True,
            "passed": True,
            "results": [{"direction": "long", "trade_eligible": True}],
        }
    if status == "BLOCKED":
        return {
            "status": "BLOCKED",
            "evaluated": True,
            "passed": False,
            "results": [
                {
                    "direction": "long",
                    "trade_eligible": False,
                    "hard_blocks": ["portfolio_risk_limit"],
                }
            ],
        }
    if status == "NOT_APPLICABLE":
        return {
            "status": "NOT_APPLICABLE",
            "evaluated": False,
            "passed": None,
            "results": [],
        }
    return {
        "status": "NOT_EVALUATED",
        "evaluated": False,
        "passed": None,
        "results": [],
    }


def _screening(
    data: DataUpdateResult,
    *,
    with_breakout: bool,
    eligibility_status: str,
    analysis: dict[str, object] | None = None,
) -> StrategyScreeningResult:
    positions = [0] if analysis is not None else []
    replay = {"0": analysis} if analysis is not None else {}
    commit_evidence = {
        "positions": positions,
        "replay_analyses": replay,
        "base_breakout_assessments": {},
        "report_bundle_seed": {"summary": {}, "series": []},
        "latest_time": AS_OF,
        "session_anchor": AS_OF,
        "cursor_before": "2026-08-21T00:00:00+00:00",
    }
    return StrategyScreeningResult(
        symbol="BTC",
        instrument_id="BTC",
        timeframe="D1",
        input_data_hash=data.result_hash,
        configuration_hash="configuration-hash",
        dataset_version="dataset-v1",
        as_of=AS_OF,
        strategy_checks={"rating": _rating()},
        turtle_breakouts=(
            ({"candidate_id": "turtle-20-long", "period": 20, "direction": "long"},)
            if with_breakout
            else ()
        ),
        eligibility=_eligibility(eligibility_status),
        screening_status="READY",
        commit_evidence=commit_evidence,
        commit_evidence_hash="commit-evidence-hash",
    )


@pytest.mark.parametrize(
    ("with_breakout", "eligibility_status", "execution_state", "reason_code"),
    [
        (False, "NOT_APPLICABLE", "WAIT", "TREND_NO_TRIGGER"),
        (
            True,
            "NOT_EVALUATED",
            "ENTRY_CANDIDATE_LONG",
            "ELIGIBILITY_NOT_CONFIRMED",
        ),
        (True, "BLOCKED", "WAIT", "RISK_BLOCKED"),
        (True, "PASSED", "ENTER_LONG", "ENTRY_CONFIRMED"),
    ],
)
def test_final_action_requires_breakout_and_confirmed_eligibility(
    with_breakout: bool,
    eligibility_status: str,
    execution_state: str,
    reason_code: str,
) -> None:
    data = _data()
    screening = _screening(
        data,
        with_breakout=with_breakout,
        eligibility_status=eligibility_status,
    )

    decision = TrendDecisionService().decide(data, screening)

    assert decision.execution_state == execution_state
    assert decision.reason_code == reason_code
    assert decision.eligibility_status == eligibility_status


def _projection(
    *,
    analysis: dict[str, object],
    with_breakout: bool,
    eligibility_status: str,
) -> dict[str, object]:
    data = _data()
    screening = _screening(
        data,
        with_breakout=with_breakout,
        eligibility_status=eligibility_status,
        analysis=analysis,
    )
    service = TrendDecisionService()
    decision = service.decide(data, screening)
    event_decisions: tuple[dict[str, object], ...] = ()
    if with_breakout:
        event_payload = service.decide(
            data,
            screening,
            event_snapshot={
                "as_of": AS_OF,
                "strategy_checks": screening.strategy_checks,
                "turtle_breakouts": screening.turtle_breakouts,
                "eligibility": screening.eligibility,
            },
        ).to_dict()
        event_payload["position"] = 0
        event_decisions = (event_payload,)
    decision = replace(decision, event_decisions=event_decisions)
    return TrendDecisionProjectionBuilder.build(
        data=data,
        screening=screening,
        decision=decision,
        data_payload=data.to_dict(),
        screening_payload=screening.to_dict(),
        context={
            "started_at": DETECTED_AT,
            "configuration": {
                "strategy_version": "corrected-v2",
                "research_mode": False,
                "backfill_signals": False,
            },
            "operational_context_hash": "operational-context-hash",
        },
        metadata={"market": "CRYPTO"},
    )


def test_grade_a_without_breakout_is_never_selected_for_notification() -> None:
    projection = _projection(
        analysis={
            "latest_bar": {"close": 110.0},
            "indicators": {"strategy_rating": {}},
            "signals": [
                {
                    "indicator": "strategy_rating",
                    "event": "grade_a_entered",
                    "direction": "long",
                    "signal_time": AS_OF,
                }
            ],
        },
        with_breakout=False,
        eligibility_status="NOT_APPLICABLE",
    )

    assert [
        event["signal_type"] for event in projection["formal_signal_events"]
    ] == ["STRATEGY_GRADE_A_LONG"]
    assert projection["execution_decision_events"] == []
    assert projection["notification_event_ids"] == []


def test_new_breakout_emits_entry_event_even_without_new_grade_transition() -> None:
    projection = _projection(
        analysis={
            "latest_bar": {"close": 110.0},
            "indicators": {"turtle_20": {"breakout_level": 100.0}},
            # The rating was already A on the previous bar, so there is no new
            # STRATEGY_GRADE_A event on this entry bar.
            "signals": [
                {
                    "indicator": "turtle_20",
                    "event": "breakout_up",
                    "direction": "long",
                    "signal_time": AS_OF,
                    "reference_value": 100.0,
                }
            ],
        },
        with_breakout=True,
        eligibility_status="PASSED",
    )

    types = [event["signal_type"] for event in projection["formal_signal_events"]]
    assert types == ["TURTLE_20_BREAKOUT", "ENTRY_DECISION_LONG"]
    assert len(projection["execution_decision_events"]) == 1
    assert len(projection["notification_event_ids"]) == 1
    assert projection["notification_signal_ids"] == projection["notification_event_ids"]
    assert projection["notification_event_ids"][0] == projection[
        "execution_decision_events"
    ][0]["event_id"]


def test_unconfirmed_entry_candidate_never_creates_formal_event() -> None:
    projection = _projection(
        analysis={
            "latest_bar": {"close": 110.0},
            "indicators": {"turtle_20": {"breakout_level": 100.0}},
            "signals": [
                {
                    "indicator": "turtle_20",
                    "event": "breakout_up",
                    "direction": "long",
                    "signal_time": AS_OF,
                    "reference_value": 100.0,
                }
            ],
        },
        with_breakout=True,
        eligibility_status="NOT_EVALUATED",
    )

    assert projection["execution_decision_events"] == []
    assert projection["notification_event_ids"] == []


def test_execution_event_identity_is_stable_across_retry_times() -> None:
    values = {
        "instrument_id": "BTC",
        "symbol": "BTC",
        "timeframe": "D1",
        "as_of": AS_OF,
        "action": "ENTER_LONG",
        "decision_hash": "decision-hash",
        "strategy_version": "corrected-v2",
        "dataset_version": "dataset-v1",
        "trigger_price": 110.0,
        "trigger_signal_ids": ("trigger-1",),
    }
    first = ExecutionDecisionEvent.create(detected_at=DETECTED_AT, **values)
    second = ExecutionDecisionEvent.create(
        detected_at="2026-08-23T00:05:00+00:00", **values
    )

    assert first.event_id == second.event_id
    assert first.event_key == second.event_key


def test_repository_deduplicates_execution_event_and_outbox(tmp_path: Path) -> None:
    repository = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    signal = ExecutionDecisionEvent.create(
        instrument_id="BTC",
        symbol="BTC",
        timeframe="D1",
        as_of=AS_OF,
        action="ENTER_LONG",
        decision_hash="decision-hash",
        strategy_version="corrected-v2",
        dataset_version="dataset-v1",
        detected_at=DETECTED_AT,
        trigger_price=110.0,
        trigger_signal_ids=("trigger-1",),
    ).to_signal_event()

    repository.start_run("run-1", DETECTED_AT)
    inserted, duplicates = repository.commit_events_and_cursor(
        [signal],
        run_id="run-1",
        instrument_id="BTC",
        timeframe="D1",
        strategy_version="corrected-v2",
        last_signal_time=AS_OF,
        notification_event_ids={signal.signal_id},
    )
    repository.start_run("run-2", "2026-08-23T00:05:00+00:00")
    inserted_again, duplicates_again = repository.commit_events_and_cursor(
        [signal],
        run_id="run-2",
        instrument_id="BTC",
        timeframe="D1",
        strategy_version="corrected-v2",
        last_signal_time=AS_OF,
        notification_event_ids={signal.signal_id},
    )

    assert len(inserted) == 1
    assert duplicates == 0
    assert inserted_again == []
    assert duplicates_again == 1
    pending = repository.pending_notifications()
    assert [event.signal_type for event in pending] == ["ENTRY_DECISION_LONG"]


def test_repository_rejects_technical_event_in_new_notification_selector(
    tmp_path: Path,
) -> None:
    repository = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    technical = SignalEvent.create(
        instrument_id="BTC",
        symbol="BTC",
        timeframe="D1",
        signal_type="STRATEGY_GRADE_A_LONG",
        direction="LONG",
        signal_time=AS_OF,
        detected_at=DETECTED_AT,
        trigger_price=110.0,
        reference_value=None,
        dataset_version="dataset-v1",
        indicator_name="strategy_rating",
    )
    repository.start_run("run-1", DETECTED_AT)

    with pytest.raises(ValueError, match="confirmed execution decision"):
        repository.commit_events_and_cursor(
            [technical],
            run_id="run-1",
            instrument_id="BTC",
            timeframe="D1",
            strategy_version="corrected-v2",
            last_signal_time=AS_OF,
            notification_event_ids={technical.signal_id},
        )


def test_schema_v1_trend_decision_hash_remains_compatible() -> None:
    result = TrendDecisionResult(
        symbol="BTC",
        instrument_id="BTC",
        timeframe="D1",
        input_data_hash="data-hash",
        input_screening_hash="screen-hash",
        as_of=AS_OF,
        trend_direction="NEUTRAL",
        execution_state="WAIT",
        decision="WAIT",
        schema_version="1",
    )
    old_payload = {
        "stage": "trend_decision",
        "schema_version": "1",
        "symbol": "BTC",
        "instrument_id": "BTC",
        "timeframe": "D1",
        "input_data_hash": "data-hash",
        "input_screening_hash": "screen-hash",
        "as_of": AS_OF,
        "trend_direction": "NEUTRAL",
        "execution_state": "WAIT",
        "decision": "WAIT",
        "confidence": {},
        "long_evidence": (),
        "short_evidence": (),
        "reverse_evidence": (),
        "risk_blocks": (),
        "event_decisions": (),
        "conclusion": "",
        "observation_only": False,
    }
    expected = hashlib.sha256(
        json.dumps(
            old_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    assert result.result_hash == expected
    assert "reason_code" not in result.to_dict()
    assert "eligibility_status" not in result.to_dict()


def _technical_grade_event() -> SignalEvent:
    return SignalEvent.create(
        instrument_id="BTC",
        symbol="BTC",
        timeframe="D1",
        signal_type="STRATEGY_GRADE_A_LONG",
        direction="LONG",
        signal_time=AS_OF,
        detected_at=DETECTED_AT,
        trigger_price=110.0,
        reference_value=None,
        dataset_version="dataset-v1",
        indicator_name="strategy_rating",
    )


def _entry_signal() -> SignalEvent:
    return ExecutionDecisionEvent.create(
        instrument_id="BTC",
        symbol="BTC",
        timeframe="D1",
        as_of=AS_OF,
        action="ENTER_LONG",
        decision_hash="decision-hash",
        strategy_version="corrected-v2",
        dataset_version="dataset-v1",
        detected_at=DETECTED_AT,
        trigger_price=110.0,
        trigger_signal_ids=("trigger-1",),
    ).to_signal_event()


def test_repository_default_outbox_selects_only_execution_events(
    tmp_path: Path,
) -> None:
    repository = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    technical = _technical_grade_event()
    entry = _entry_signal()
    repository.start_run("run-1", DETECTED_AT)

    inserted, duplicates = repository.commit_events_and_cursor(
        [technical, entry],
        run_id="run-1",
        instrument_id="BTC",
        timeframe="D1",
        strategy_version="corrected-v2",
        last_signal_time=AS_OF,
    )

    assert len(inserted) == 2
    assert duplicates == 0
    assert [event.signal_type for event in repository.pending_notifications()] == [
        "ENTRY_DECISION_LONG"
    ]


def test_legacy_notification_parameter_name_keeps_execution_only_semantics(
    tmp_path: Path,
) -> None:
    repository = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    technical = _technical_grade_event()
    repository.start_run("run-1", DETECTED_AT)

    with pytest.raises(ValueError, match="confirmed execution decision"):
        repository.commit_events_and_cursor(
            [technical],
            run_id="run-1",
            instrument_id="BTC",
            timeframe="D1",
            strategy_version="corrected-v2",
            last_signal_time=AS_OF,
            notification_signal_ids={technical.signal_id},
        )


def test_short_entry_path_is_symmetric() -> None:
    data = _data()
    screening = _screening(
        data,
        with_breakout=True,
        eligibility_status="PASSED",
        analysis={
            "latest_bar": {"close": 90.0},
            "indicators": {"turtle_20": {"breakout_level": 100.0}},
            "signals": [
                {
                    "indicator": "turtle_20",
                    "event": "breakout_down",
                    "direction": "short",
                    "signal_time": AS_OF,
                    "reference_value": 100.0,
                }
            ],
        },
    )
    screening = replace(
        screening,
        strategy_checks={
            "rating": {
                **_rating(),
                "direction": "short",
                "family_votes": {
                    "sma": "short",
                    "ema": "short",
                    "macd": "short",
                },
            }
        },
        turtle_breakouts=(
            {"candidate_id": "turtle-20-short", "period": 20, "direction": "short"},
        ),
        eligibility={
            "status": "PASSED",
            "evaluated": True,
            "passed": True,
            "results": [{"direction": "short", "trade_eligible": True}],
        },
    )
    service = TrendDecisionService()
    decision = service.decide(data, screening)
    event_payload = decision.to_dict()
    event_payload["position"] = 0
    decision = replace(decision, event_decisions=(event_payload,))

    projection = TrendDecisionProjectionBuilder.build(
        data=data,
        screening=screening,
        decision=decision,
        data_payload=data.to_dict(),
        screening_payload=screening.to_dict(),
        context={
            "started_at": DETECTED_AT,
            "configuration": {
                "strategy_version": "corrected-v2",
                "research_mode": False,
                "backfill_signals": False,
            },
            "operational_context_hash": "operational-context-hash",
        },
        metadata={"market": "CRYPTO"},
    )

    assert decision.execution_state == "ENTER_SHORT"
    assert [
        item["event_type"] for item in projection["execution_decision_events"]
    ] == ["ENTRY_DECISION_SHORT"]
    assert len(projection["notification_event_ids"]) == 1


def test_execution_event_dict_round_trip_and_identity_validation() -> None:
    event = ExecutionDecisionEvent.create(
        instrument_id="BTC",
        symbol="BTC",
        timeframe="D1",
        as_of=AS_OF,
        action="ENTER_LONG",
        decision_hash="decision-hash",
        strategy_version="corrected-v2",
        dataset_version="dataset-v1",
        detected_at=DETECTED_AT,
        trigger_price=110.0,
        trigger_signal_ids=("trigger-1",),
    )

    restored = ExecutionDecisionEvent(**event.to_dict())
    assert restored == event

    tampered = event.to_dict()
    tampered["event_id"] = "tampered"
    with pytest.raises(ValueError, match="identity"):
        ExecutionDecisionEvent(**tampered)


def test_execution_event_builder_rejects_inconsistent_enter_decision() -> None:
    data = _data()
    screening = _screening(
        data,
        with_breakout=True,
        eligibility_status="PASSED",
    )
    decision = TrendDecisionService().decide(data, screening)
    malformed = replace(decision, eligibility_status="NOT_EVALUATED")
    trigger = SignalEvent.create(
        instrument_id="BTC",
        symbol="BTC",
        timeframe="D1",
        signal_type="TURTLE_20_BREAKOUT",
        direction="LONG",
        signal_time=AS_OF,
        detected_at=DETECTED_AT,
        trigger_price=110.0,
        reference_value=100.0,
        dataset_version="dataset-v1",
        indicator_name="turtle_20",
    )

    with pytest.raises(ValueError, match="confirmed, unblocked"):
        build_execution_decision_event(
            malformed,
            trigger_events=(trigger,),
            dataset_version="dataset-v1",
            detected_at=DETECTED_AT,
            strategy_version="corrected-v2",
        )


def test_legacy_staging_notification_ids_are_safely_suppressed() -> None:
    technical = _technical_grade_event()
    selected, suppressed = _resolve_projection_notification_ids(
        {"notification_signal_ids": [technical.signal_id]},
        {technical.signal_id: technical},
        symbol="BTC",
    )

    assert selected == set()
    assert suppressed == (technical.signal_id,)


def test_new_staging_projection_rejects_technical_notification_ids() -> None:
    technical = _technical_grade_event()
    with pytest.raises(ValueError, match="confirmed execution"):
        _resolve_projection_notification_ids(
            {"notification_event_ids": [technical.signal_id]},
            {technical.signal_id: technical},
            symbol="BTC",
        )


def test_atomic_daily_commit_enqueues_only_selected_execution_event(
    tmp_path: Path,
) -> None:
    repository = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    technical = _technical_grade_event()
    entry = _entry_signal()
    row: dict[str, object] = {
        "symbol": "BTC",
        "instrument_id": "BTC",
        "run_status": "updated",
        "signals_detected": 2,
    }

    inserted, summary = repository.commit_daily_run(
        run_id="run-atomic",
        started_at=DETECTED_AT,
        finished_at="2026-08-23T00:01:00+00:00",
        commits=[
            {
                "instrument_id": "BTC",
                "timeframe": "D1",
                "strategy_version": "corrected-v2",
                "expected_cursor": None,
                "last_signal_time": AS_OF,
                "session_anchor": AS_OF,
                "events": [technical, entry],
                "enqueue_notifications": True,
                "notification_event_ids": {entry.signal_id},
                "row": row,
            }
        ],
        records=[row],
    )

    assert len(inserted) == 2
    assert summary["signals_new"] == 2
    assert [event.signal_type for event in repository.pending_notifications()] == [
        "ENTRY_DECISION_LONG"
    ]
