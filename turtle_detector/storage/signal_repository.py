"""Persistence boundary used for state transitions and alert deduplication."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Protocol

from ..models import DetectorState, Direction, PositionStatus, TurtleSignal


class SignalRepository(Protocol):
    def load_state(self, symbol: str, timeframe: str) -> DetectorState | None: ...

    def save_state(self, state: DetectorState) -> None: ...

    def is_new(self, signal: TurtleSignal) -> bool: ...

    def save_signal(self, signal: TurtleSignal) -> None: ...


class InMemorySignalRepository:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], DetectorState] = {}
        self.signal_keys: set[str] = set()
        self.signals: list[TurtleSignal] = []

    def load_state(self, symbol: str, timeframe: str) -> DetectorState | None:
        return self.states.get((symbol, timeframe))

    def save_state(self, state: DetectorState) -> None:
        self.states[(state.symbol, state.timeframe)] = state

    def is_new(self, signal: TurtleSignal) -> bool:
        return signal_key(signal) not in self.signal_keys

    def save_signal(self, signal: TurtleSignal) -> None:
        self.signal_keys.add(signal_key(signal))
        self.signals.append(signal)


class JsonSignalRepository(InMemorySignalRepository):
    """Small-file repository suitable for scheduled scanners, not high-frequency use."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._load()

    def save_state(self, state: DetectorState) -> None:
        super().save_state(state)
        self._flush()

    def save_signal(self, signal: TurtleSignal) -> None:
        super().save_signal(signal)
        self._flush()

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for raw in payload.get("states", []):
            values = dict(raw)
            values["status"] = PositionStatus(values["status"])
            values["direction"] = Direction(values["direction"])
            state = DetectorState(**values)
            self.states[(state.symbol, state.timeframe)] = state
        self.signal_keys.update(payload.get("signal_keys", []))

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        states = []
        for state in self.states.values():
            row = asdict(state)
            row["status"] = state.status.value
            row["direction"] = state.direction.value
            states.append(row)
        payload = {"states": states, "signal_keys": sorted(self.signal_keys)}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def signal_key(signal: TurtleSignal) -> str:
    return "|".join(
        [
            signal.instrument,
            signal.timeframe,
            signal.signal_type.value,
            signal.direction.value,
            signal.signal_time,
            f"{signal.trigger_price:.10g}",
        ]
    )
