from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from inv_trend.core.events import DecisionEvent, EventType
from inv_trend.core.serializers import serialize_event
from inv_trend.adapters.detector.decisions import SIGNAL_TYPE_MAP, TransitionKind, decision_key
from inv_trend.adapters.detector.models import Direction, PositionStatus, SignalType, TurtleSignal
from inv_trend.adapters.detector.scan_state import PositionState, ScanCursor
from inv_trend.adapters.detector.storage.signal_repository import JsonSignalRepository
from inv_trend.adapters.multi_asset.models.domain import LONG, AssetSpec, Order, PortfolioState, TurtleRules
from inv_trend.adapters.multi_asset.models.order_intent import PendingOrderIntent, ReservationBook
from inv_trend.adapters.multi_asset.risk.expiry import ExpiryPolicy
from inv_trend.adapters.multi_asset.risk.fill_guard import FillRiskGuard
from inv_trend.adapters.multi_asset.strategy.budget_policy import PortfolioBudgetPolicy


def signal(signal_type: SignalType = SignalType.SYSTEM1_BREAKOUT) -> TurtleSignal:
    return TurtleSignal(
        symbol="BTC", instrument="BTCUSDT_BINANCE_SPOT", market="crypto", timeframe="D1",
        signal_type=signal_type, raw_signal_type=signal_type, direction=Direction.LONG,
        signal_time="2024-01-02T00:00:00+00:00", trigger_price=100.0,
        channel_high=99.0, channel_low=90.0, atr=5.0, atr_pct=0.05, stop_price=90.0,
        next_add_price=102.5, distance_to_breakout_atr=0.2, volatility_percentile=0.5,
        suggested_risk_unit=0.01, trend_status="uptrend", confirmation_status="none",
        data_source="test", generated_at="2024-01-02T00:00:00+00:00", tradeable=True,
    )


def order(qty: float = 10.0) -> Order:
    return Order("BTC", "open", LONG, qty, "breakout", "fast", 100.0, 5.0, 90.0,
                 risk_1n_pct=qty * 5 / 1_000)


def test_all_signal_types_are_classified_without_retest_or_overextended_execution():
    assert set(SIGNAL_TYPE_MAP) == set(SignalType)
    assert SIGNAL_TYPE_MAP[SignalType.RETEST_CONFIRMED] is TransitionKind.OBSERVE
    assert SIGNAL_TYPE_MAP[SignalType.OVEREXTENDED] is TransitionKind.OBSERVE


def test_decision_key_uses_signal_identity_not_cursor_time():
    first = decision_key(signal())
    changed = decision_key(signal(SignalType.SYSTEM2_BREAKOUT))
    assert first != changed
    assert "2024-01-02T00:00:00+00:00" in first


def test_atomic_json_commit_round_trips_cursor_position_signal_event_and_outbox(tmp_path: Path):
    path = tmp_path / "state.json"
    repository = JsonSignalRepository(path)
    item = signal()
    assert repository.commit(
        cursor=ScanCursor("BTC", "D1", item.signal_time, decision_key(item)),
        position=PositionState(status=PositionStatus.ENTERED, direction=Direction.LONG, holding_bars=3),
        signal=item,
        decision_event={"event": "committed"},
        outbox_record={"key": decision_key(item)},
    )
    assert not repository.commit(
        cursor=ScanCursor("BTC", "D1"), position=PositionState(), signal=item
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2"
    restored = JsonSignalRepository(path)
    assert restored.load_cursor("BTC", "D1").last_decision_key == decision_key(item)
    assert restored.load_position("BTC", "D1").holding_bars == 3
    assert len(restored.outbox) == 1


def test_event_serialization_is_json_safe_for_nan_enum_timestamp_and_context():
    event = DecisionEvent(
        event_id="e", run_id="r", event_time="now", event_type=EventType.SIGNAL_CANDIDATE,
        stage="test", metrics={"nan": float("nan"), "inf": np.inf},
        context={"time": pd.Timestamp("2024-01-01", tz="UTC"), "type": EventType.RUN_STARTED},
    )
    result = serialize_event(event)
    assert result["event_schema_version"] == "1.0"
    assert result["metrics"] == {"nan": None, "inf": None}
    assert json.loads(json.dumps(result, allow_nan=False))["context"]["type"] == "RUN_STARTED"


def test_fill_guard_excludes_only_current_intent_and_scales_by_capacity():
    spec = AssetSpec("BTC", "crypto", "crypto", unit_1n_risk_pct=0.01,
                     max_symbol_1n_risk_pct=0.02, max_symbol_leverage=10.0)
    rules = TurtleRules(max_total_1n_risk_pct=0.02, max_direction_1n_risk_pct=0.02,
                        default_cluster_1n_risk_pct=0.02, max_total_leverage=10.0,
                        max_direction_leverage=10.0, default_cluster_leverage=10.0)
    book = ReservationBook()
    first = PendingOrderIntent(order(), "a", "a", 20, 99.0, 10, 0.01, 1.0)
    second = PendingOrderIntent(order(), "a", "a", 20, 99.0, 10, 0.01, 1.0)
    book.reserve(first)
    book.reserve(second)
    decision = FillRiskGuard(PortfolioBudgetPolicy(rules, {"BTC": spec})).validate(
        first, 100.0, PortfolioState(), book, 1_000.0, {"BTC": 100.0}
    )
    assert decision.allowed
    assert decision.approved_qty == pytest.approx(2.0)


def test_expiry_first_attempt_is_allowed_and_uses_previous_bar_not_current_close():
    intent = PendingOrderIntent(order(), "a", "a", 20, 99.0, 10, 0.01, 1.0)
    policy = ExpiryPolicy(max_fill_attempts=1, max_fill_gap_n=2.0)
    assert policy.check_before_fill(intent, {"close": 100, "high_20": 99, "n": 5}, 101).status == "pending"
    intent.fill_attempts_completed = 1
    assert policy.check_before_fill(intent, None, 101).status == "expired"
