"""Application boundary for one-shot detector scans without CLI coupling."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from inv_trend.adapters.detector.decisions import DetectionDecision
from inv_trend.adapters.detector.engine.candidate_detector import CandidateDetector
from inv_trend.adapters.detector.models import AssetConfig, StrategyConfig
from inv_trend.adapters.detector.scan_state import PositionState, ScanCursor
from inv_trend.adapters.detector.storage.signal_repository import SignalRepository


@dataclass(frozen=True)
class DetectorRun:
    decision: DetectionDecision
    committed: bool


class DetectorService:
    """Load state, evaluate a pure candidate, then commit it atomically."""

    def __init__(self, config: StrategyConfig, repository: SignalRepository) -> None:
        self.detector = CandidateDetector(config)
        self.repository = repository

    def scan(self, bars: pd.DataFrame, asset: AssetConfig, timeframe: str) -> DetectorRun:
        identity = asset.symbol, timeframe.upper()
        cursor = self.repository.load_cursor(*identity) or ScanCursor(*identity)
        position = self.repository.load_position(*identity) or PositionState()
        decision = self.detector.detect(bars, asset, timeframe, cursor, position)
        if cursor.last_decision_key == decision.next_cursor.last_decision_key:
            return DetectorRun(decision=decision, committed=False)
        signal = decision.signal if decision.execution_transition else None
        committed = self.repository.commit(
            cursor=decision.next_cursor,
            position=decision.next_position,
            signal=signal,
            decision_event={"action": decision.transition_kind.value},
            outbox_record={"signal_key": decision.next_cursor.last_decision_key} if signal else None,
        )
        return DetectorRun(decision=decision, committed=committed)
