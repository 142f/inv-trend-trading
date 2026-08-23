"""Focused compatibility coverage for the consolidated detector workflow."""

from __future__ import annotations

import pandas as pd

from inv_trend.adapters.detector.decisions import DetectionDecision, TransitionKind
from inv_trend.adapters.detector.models import (
    AssetConfig,
    ConfirmationStatus,
    Direction,
    Market,
    SignalType,
    StrategyConfig,
    TurtleSignal,
)
from inv_trend.adapters.detector.scan_state import PositionState, ScanCursor
from inv_trend.adapters.detector.storage.signal_repository import InMemorySignalRepository
from inv_trend.application.detector_scan_service import DetectorScanService, ScanResult
from inv_trend.application.detector_scan_workflow import DetectorScanWorkflow
from inv_trend.application.detector_service import DetectorRun, DetectorService


class _StaticDetector:
    def __init__(self, decision: DetectionDecision) -> None:
        self.decision = decision
        self.calls = 0

    def detect(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        cursor: ScanCursor,
        position: PositionState,
    ) -> DetectionDecision:
        self.calls += 1
        return self.decision


def _asset() -> AssetConfig:
    return AssetConfig(
        symbol="TEST",
        instrument="TEST-USD",
        market=Market.CRYPTO,
        data_source="fixture",
        timeframes=("D1",),
    )


def _decision(signal_type: SignalType, transition: TransitionKind) -> DetectionDecision:
    signal = TurtleSignal(
        symbol="TEST",
        instrument="TEST-USD",
        market="crypto",
        timeframe="D1",
        signal_type=signal_type,
        raw_signal_type=signal_type,
        direction=Direction.LONG,
        signal_time="2024-01-02T00:00:00+00:00",
        trigger_price=100.0,
        channel_high=99.0,
        channel_low=90.0,
        atr=5.0,
        atr_pct=0.05,
        stop_price=90.0,
        next_add_price=102.5,
        distance_to_breakout_atr=0.2,
        volatility_percentile=0.5,
        suggested_risk_unit=0.01,
        trend_status="uptrend",
        confirmation_status=ConfirmationStatus.NONE,
        data_source="fixture",
        generated_at="2024-01-02T00:00:00+00:00",
        tradeable=True,
    )
    return DetectionDecision(
        signal=signal,
        next_cursor=ScanCursor(
            "TEST",
            "D1",
            last_processed_time=signal.signal_time,
            last_decision_key=f"decision-{signal_type.value}",
        ),
        next_position=PositionState(),
        transition_kind=transition,
    )


def test_legacy_facades_share_the_canonical_workflow_for_execution_transitions() -> None:
    decision = _decision(SignalType.SYSTEM1_BREAKOUT, TransitionKind.ENTRY)
    legacy_repository = InMemorySignalRepository()
    scan_repository = InMemorySignalRepository()
    legacy = DetectorService(StrategyConfig(), legacy_repository)
    legacy.detector = _StaticDetector(decision)
    scan = DetectorScanService(_StaticDetector(decision), scan_repository)

    legacy_result = legacy.scan(pd.DataFrame(), _asset(), "D1")
    scan_result = scan.scan(pd.DataFrame(), _asset(), "D1")

    assert isinstance(legacy, DetectorScanWorkflow)
    assert isinstance(scan, DetectorScanWorkflow)
    assert isinstance(legacy_result, DetectorRun)
    assert isinstance(scan_result, ScanResult)
    assert legacy_result.decision == scan_result.decision == decision
    assert legacy_result.committed and scan_result.committed
    assert legacy_repository.signals == scan_repository.signals == [decision.signal]
    assert legacy_repository.decision_events == scan_repository.decision_events
    assert legacy_repository.outbox == scan_repository.outbox

    assert not legacy.scan(pd.DataFrame(), _asset(), "D1").committed
    assert not scan.scan(pd.DataFrame(), _asset(), "D1").committed


def test_turtle_detect_facade_keeps_execution_only_signal_policy() -> None:
    decision = _decision(SignalType.APPROACHING_BREAKOUT, TransitionKind.OBSERVE)
    legacy_repository = InMemorySignalRepository()
    scan_repository = InMemorySignalRepository()
    legacy = DetectorService(StrategyConfig(), legacy_repository)
    legacy.detector = _StaticDetector(decision)
    scan = DetectorScanService(_StaticDetector(decision), scan_repository)

    legacy_result = legacy.scan(pd.DataFrame(), _asset(), "D1")
    scan_result = scan.scan(pd.DataFrame(), _asset(), "D1")

    assert legacy_result.committed and scan_result.committed
    assert legacy_repository.load_cursor("TEST", "D1") == decision.next_cursor
    assert legacy_repository.signals == []
    assert legacy_repository.outbox == []
    assert scan_repository.signals == [decision.signal]
    assert scan_repository.outbox == [{"signal_key": decision.next_cursor.last_decision_key}]


def test_canonical_workflow_preserves_paper_mode_retry_before_cursor_commit() -> None:
    decision = _decision(SignalType.SYSTEM1_BREAKOUT, TransitionKind.ENTRY)
    repository = InMemorySignalRepository()
    service = DetectorScanService(
        _StaticDetector(decision),
        repository,
        mode="paper",
    )

    result = service.scan(pd.DataFrame(), _asset(), "D1")

    assert not result.committed
    assert result.deferred
    assert repository.load_cursor("TEST", "D1") is None
    assert repository.signals == []
