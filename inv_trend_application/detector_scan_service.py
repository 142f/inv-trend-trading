"""Application service for atomic detector scans and eligibility gating."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from turtle_detector.decisions import DetectionDecision
from turtle_detector.eligibility.checker import AccountSnapshot, TradeEligibilityChecker
from turtle_detector.eligibility.models import EligibilityVerdict
from turtle_detector.engine.candidate_detector import CandidateDetector
from turtle_detector.models import AssetConfig, SignalType
from turtle_detector.scan_state import PositionState, ScanCursor
from turtle_detector.storage.signal_repository import SignalRepository


@dataclass(frozen=True)
class ScanResult:
    decision: DetectionDecision
    committed: bool
    notified: bool = False
    deferred: bool = False


class DetectorScanService:
    def __init__(
        self,
        detector: CandidateDetector,
        repository: SignalRepository,
        checker: TradeEligibilityChecker | None = None,
        notifier: object | None = None,
        mode: Literal["signal_only", "research", "backtest", "paper", "live"] = "signal_only",
    ) -> None:
        self.detector = detector
        self.repository = repository
        self.checker = checker
        self.notifier = notifier
        self.mode = mode

    def scan(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        account: AccountSnapshot | None = None,
    ) -> ScanResult:
        key = (asset.symbol, timeframe.upper())
        cursor = self.repository.load_cursor(*key) or ScanCursor(*key)
        position = self.repository.load_position(*key) or PositionState()
        decision = self.detector.detect(bars, asset, timeframe, cursor, position)
        if cursor.last_decision_key == decision.next_cursor.last_decision_key:
            return ScanResult(decision, committed=False)

        # Entry-like transitions must never manufacture an execution state in
        # paper/live mode without an account snapshot for portfolio risk checks.
        if decision.requires_eligibility and self.mode in {"paper", "live"} and account is None:
            # Do not advance the cursor: the same completed bar must be retryable
            # once a trustworthy account snapshot arrives.
            return ScanResult(decision, committed=False, deferred=True)

        if decision.requires_eligibility and self.checker is not None:
            eligibility = self.checker.evaluate(
                decision.signal,
                bars,
                asset,
                account,
                backtest_validated=self.mode == "backtest",
                execution_ready=self.mode in {"paper", "live"},
                require_account=self.mode in {"paper", "live"},
            )
            if not eligibility.trade_eligible:
                if self.checker.verdict(eligibility) is EligibilityVerdict.DEFER_RETRYABLE:
                    return ScanResult(decision, committed=False, deferred=True)
                committed = self.repository.commit(
                    cursor=decision.next_cursor,
                    position=position,
                    signal=decision.signal,
                    decision_event={"action": "eligibility_rejected", "reasons": [x.value for x in eligibility.hard_blocks]},
                )
                return ScanResult(decision, committed)

        signal = decision.signal if decision.signal.signal_type is not SignalType.NO_SIGNAL else None
        committed = self.repository.commit(
            cursor=decision.next_cursor,
            position=decision.next_position,
            signal=signal,
            decision_event={"action": decision.transition_kind.value},
            outbox_record={"signal_key": decision.next_cursor.last_decision_key} if signal else None,
        )
        notified = False
        if committed and signal is not None and self.notifier is not None:
            self.notifier.notify(signal)
            notified = True
        return ScanResult(decision, committed, notified)
