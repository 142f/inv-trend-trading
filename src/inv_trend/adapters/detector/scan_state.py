"""Separated scan progress and position state; keeps models.py import compatibility."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Direction, PositionStatus


@dataclass(frozen=True)
class ScanCursor:
    symbol: str
    timeframe: str
    last_processed_time: str | None = None
    last_decision_key: str | None = None


@dataclass(frozen=True)
class PositionState:
    status: PositionStatus = PositionStatus.FLAT
    direction: Direction = Direction.NONE
    system: int = 0
    entry_time: str | None = None
    entry_price: float | None = None
    breakout_level: float | None = None
    atr_at_entry: float | None = None
    stop_price: float | None = None
    next_add_price: float | None = None
    additions: int = 0
    holding_bars: int = 0
    last_system1_won: bool = False
    last_signal_key: str | None = None
    pending_since: str | None = None
