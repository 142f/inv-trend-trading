"""Canonical application workflow for one detector scan.

The workflow owns the shared read -> detect -> eligibility -> commit sequence.
Legacy services adapt their construction and result contracts around it so that
the detector CLI and callers keep their existing semantics without maintaining
two independent transaction implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

import pandas as pd

from inv_trend.adapters.detector.decisions import DetectionDecision
from inv_trend.adapters.detector.eligibility.checker import (
    AccountSnapshot,
    TradeEligibilityChecker,
)
from inv_trend.adapters.detector.eligibility.models import EligibilityVerdict
from inv_trend.adapters.detector.engine.candidate_detector import CandidateDetector
from inv_trend.adapters.detector.models import AssetConfig, SignalType, TurtleSignal
from inv_trend.adapters.detector.scan_state import PositionState, ScanCursor
from inv_trend.adapters.detector.storage.signal_repository import SignalRepository


ScanMode = Literal["signal_only", "research", "backtest", "paper", "live"]


class SignalPersistencePolicy(str, Enum):
    """Compatibility policy for which detector observations become signals."""

    EXECUTION_TRANSITIONS = "execution_transitions"
    ALL_NON_NO_SIGNAL = "all_non_no_signal"


class SignalNotifier(Protocol):
    """Minimal notification boundary used after a successful commit."""

    def notify(self, signal: TurtleSignal) -> object: ...


@dataclass(frozen=True)
class DetectorScanOutcome:
    """Result of a single canonical detector workflow invocation."""

    decision: DetectionDecision
    committed: bool
    notified: bool = False
    deferred: bool = False


class DetectorScanWorkflow:
    """Evaluate one candidate and commit its state transition atomically.

    The only intentionally variable legacy behavior is signal persistence:
    ``DetectorService`` historically saved only execution transitions, whereas
    ``DetectorScanService`` saved every non-``NO_SIGNAL`` observation.  Keeping
    that policy explicit lets both public compatibility facades share every
    other part of the workflow.
    """

    def __init__(
        self,
        detector: CandidateDetector,
        repository: SignalRepository,
        checker: TradeEligibilityChecker | None = None,
        notifier: SignalNotifier | None = None,
        mode: ScanMode = "signal_only",
        *,
        signal_persistence: SignalPersistencePolicy = SignalPersistencePolicy.ALL_NON_NO_SIGNAL,
    ) -> None:
        self.detector = detector
        self.repository = repository
        self.checker = checker
        self.notifier = notifier
        self.mode = mode
        self.signal_persistence = signal_persistence

    def scan(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        account: AccountSnapshot | None = None,
    ) -> DetectorScanOutcome:
        key = (asset.symbol, timeframe.upper())
        cursor = self.repository.load_cursor(*key) or ScanCursor(*key)
        position = self.repository.load_position(*key) or PositionState()
        decision = self.detector.detect(bars, asset, timeframe, cursor, position)

        if cursor.last_decision_key == decision.next_cursor.last_decision_key:
            return DetectorScanOutcome(decision=decision, committed=False)

        if decision.requires_eligibility and self.mode in {"paper", "live"} and account is None:
            # Do not advance the cursor: the completed bar remains retryable
            # when the caller obtains a trustworthy account snapshot.
            return DetectorScanOutcome(decision=decision, committed=False, deferred=True)

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
                    return DetectorScanOutcome(decision=decision, committed=False, deferred=True)
                committed = self.repository.commit(
                    cursor=decision.next_cursor,
                    position=position,
                    signal=decision.signal,
                    decision_event={
                        "action": "eligibility_rejected",
                        "reasons": [reason.value for reason in eligibility.hard_blocks],
                    },
                )
                return DetectorScanOutcome(decision=decision, committed=committed)

        signal = decision.signal if self._should_persist_signal(decision) else None
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
        return DetectorScanOutcome(decision=decision, committed=committed, notified=notified)

    def _should_persist_signal(self, decision: DetectionDecision) -> bool:
        if self.signal_persistence is SignalPersistencePolicy.EXECUTION_TRANSITIONS:
            return decision.execution_transition
        return decision.signal.signal_type is not SignalType.NO_SIGNAL


__all__ = [
    "DetectorScanOutcome",
    "DetectorScanWorkflow",
    "ScanMode",
    "SignalNotifier",
    "SignalPersistencePolicy",
]
