"""Explicit detector transition classification, independent of storage/execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from .models import SignalType, TurtleSignal
from .scan_state import PositionState, ScanCursor


class TransitionKind(str, Enum):
    OBSERVE = "observe"
    ENTRY = "entry"
    ADD = "add"
    CONFIRM = "confirm"
    EXIT = "exit"
    STOP = "stop"
    INVALIDATE = "invalidate"


SIGNAL_TYPE_MAP = {
    SignalType.NO_SIGNAL: TransitionKind.OBSERVE,
    SignalType.APPROACHING_BREAKOUT: TransitionKind.OBSERVE,
    SignalType.OVEREXTENDED: TransitionKind.OBSERVE,
    SignalType.RETEST_CONFIRMED: TransitionKind.OBSERVE,
    SignalType.SYSTEM1_BREAKOUT: TransitionKind.ENTRY,
    SignalType.SYSTEM2_BREAKOUT: TransitionKind.ENTRY,
    SignalType.CLOSE_CONFIRMED: TransitionKind.CONFIRM,
    SignalType.PYRAMID_ADD: TransitionKind.ADD,
    SignalType.FALSE_BREAKOUT: TransitionKind.INVALIDATE,
    SignalType.TREND_INVALIDATED: TransitionKind.INVALIDATE,
    SignalType.EXIT_SIGNAL: TransitionKind.EXIT,
    SignalType.ATR_STOP: TransitionKind.STOP,
}


@dataclass(frozen=True)
class DetectionDecision:
    signal: TurtleSignal
    next_cursor: ScanCursor
    next_position: PositionState
    transition_kind: TransitionKind
    prepared_row: pd.Series | None = None

    @property
    def requires_eligibility(self) -> bool:
        return self.transition_kind in {TransitionKind.ENTRY, TransitionKind.ADD, TransitionKind.CONFIRM}

    @property
    def execution_transition(self) -> bool:
        return self.transition_kind is not TransitionKind.OBSERVE


def decision_key(signal: TurtleSignal) -> str:
    return "|".join((signal.instrument, signal.timeframe, signal.signal_time, signal.signal_type.value,
                     signal.direction.value, f"{signal.trigger_price:.10g}"))
