"""Compatibility adapter that separates candidate detection from committing it."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ..decisions import DetectionDecision, SIGNAL_TYPE_MAP, decision_key
from ..models import AssetConfig, DetectorState, StrategyConfig
from ..scan_state import PositionState, ScanCursor
from .scanner import TurtleScanner


def _to_detector_state(cursor: ScanCursor, position: PositionState) -> DetectorState:
    return DetectorState(
        symbol=cursor.symbol,
        timeframe=cursor.timeframe,
        status=position.status,
        direction=position.direction,
        system=position.system,
        entry_time=position.entry_time,
        entry_price=position.entry_price,
        breakout_level=position.breakout_level,
        atr_at_entry=position.atr_at_entry,
        stop_price=position.stop_price,
        next_add_price=position.next_add_price,
        additions=position.additions,
        holding_bars=position.holding_bars,
        last_system1_won=position.last_system1_won,
        last_signal_key=position.last_signal_key,
        pending_since=position.pending_since,
        last_processed_time=cursor.last_processed_time,
    )


def _to_position(state: DetectorState) -> PositionState:
    return PositionState(
        status=state.status,
        direction=state.direction,
        system=state.system,
        entry_time=state.entry_time,
        entry_price=state.entry_price,
        breakout_level=state.breakout_level,
        atr_at_entry=state.atr_at_entry,
        stop_price=state.stop_price,
        next_add_price=state.next_add_price,
        additions=state.additions,
        holding_bars=state.holding_bars,
        last_system1_won=state.last_system1_won,
        last_signal_key=state.last_signal_key,
        pending_since=state.pending_since,
    )


class CandidateDetector:
    """Produces an immutable decision without any repository or notifier side effect."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.scanner = TurtleScanner(config=config)

    def prepare(self, bars: pd.DataFrame, asset: AssetConfig, timeframe: str) -> pd.DataFrame:
        return self.scanner.prepare(bars, asset, timeframe)

    def detect(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        cursor: ScanCursor | None = None,
        position: PositionState | None = None,
    ) -> DetectionDecision:
        identity = (asset.symbol, timeframe.upper())
        cursor = cursor or ScanCursor(*identity)
        position = position or PositionState()
        result = self.scanner.detect(
            bars, asset, timeframe, _to_detector_state(cursor, position)
        )
        next_cursor = replace(cursor, last_processed_time=result.state.last_processed_time)
        next_position = _to_position(result.state)
        key = decision_key(result.signal)
        next_cursor = replace(next_cursor, last_decision_key=key)
        next_position = replace(next_position, last_signal_key=key)
        return DetectionDecision(
            signal=result.signal,
            next_cursor=next_cursor,
            next_position=next_position,
            transition_kind=SIGNAL_TYPE_MAP[result.signal.signal_type],
            prepared_row=self.scanner.prepare(bars, asset, timeframe).iloc[-1],
        )
