"""Compatibility facade for the original one-shot detector service."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from inv_trend.adapters.detector.decisions import DetectionDecision
from inv_trend.adapters.detector.engine.candidate_detector import CandidateDetector
from inv_trend.adapters.detector.models import AssetConfig, StrategyConfig
from inv_trend.adapters.detector.storage.signal_repository import SignalRepository

from .detector_scan_workflow import DetectorScanWorkflow, SignalPersistencePolicy


@dataclass(frozen=True)
class DetectorRun:
    """Legacy result shape returned by :class:`DetectorService`."""

    decision: DetectionDecision
    committed: bool


class DetectorService(DetectorScanWorkflow):
    """Backward-compatible facade for ``turtle-detect`` callers.

    It preserves the historic constructor and signal policy: only execution
    transitions are persisted as signals and placed into the outbox.
    """

    def __init__(self, config: StrategyConfig, repository: SignalRepository) -> None:
        super().__init__(
            detector=CandidateDetector(config),
            repository=repository,
            signal_persistence=SignalPersistencePolicy.EXECUTION_TRANSITIONS,
        )

    def scan(self, bars: pd.DataFrame, asset: AssetConfig, timeframe: str) -> DetectorRun:
        outcome = super().scan(bars, asset, timeframe)
        return DetectorRun(decision=outcome.decision, committed=outcome.committed)


__all__ = ["DetectorRun", "DetectorService"]
