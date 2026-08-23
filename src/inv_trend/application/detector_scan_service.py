"""Compatibility facade for the legacy detector scan service."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from inv_trend.adapters.detector.decisions import DetectionDecision
from inv_trend.adapters.detector.eligibility.checker import (
    AccountSnapshot,
    TradeEligibilityChecker,
)
from inv_trend.adapters.detector.engine.candidate_detector import CandidateDetector
from inv_trend.adapters.detector.models import AssetConfig
from inv_trend.adapters.detector.storage.signal_repository import SignalRepository

from .detector_scan_workflow import (
    DetectorScanWorkflow,
    ScanMode,
    SignalNotifier,
    SignalPersistencePolicy,
)


@dataclass(frozen=True)
class ScanResult:
    """Legacy result shape returned by :class:`DetectorScanService`."""

    decision: DetectionDecision
    committed: bool
    notified: bool = False
    deferred: bool = False


class DetectorScanService(DetectorScanWorkflow):
    """Backward-compatible facade with eligibility and notification support."""

    def __init__(
        self,
        detector: CandidateDetector,
        repository: SignalRepository,
        checker: TradeEligibilityChecker | None = None,
        notifier: SignalNotifier | None = None,
        mode: ScanMode = "signal_only",
    ) -> None:
        super().__init__(
            detector=detector,
            repository=repository,
            checker=checker,
            notifier=notifier,
            mode=mode,
            signal_persistence=SignalPersistencePolicy.ALL_NON_NO_SIGNAL,
        )

    def scan(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        account: AccountSnapshot | None = None,
    ) -> ScanResult:
        outcome = super().scan(bars, asset, timeframe, account)
        return ScanResult(
            decision=outcome.decision,
            committed=outcome.committed,
            notified=outcome.notified,
            deferred=outcome.deferred,
        )


__all__ = ["DetectorScanService", "ScanResult"]
