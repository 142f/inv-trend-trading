"""Persistence boundary used for state transitions and alert deduplication."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Protocol

from ..models import DetectorState, Direction, PositionStatus, TurtleSignal
from ..scan_state import PositionState, ScanCursor


class SignalRepository(Protocol):
    def load_state(self, symbol: str, timeframe: str) -> DetectorState | None: ...

    def save_state(self, state: DetectorState) -> None: ...

    def is_new(self, signal: TurtleSignal) -> bool: ...

    def save_signal(self, signal: TurtleSignal) -> None: ...

    def load_cursor(self, symbol: str, timeframe: str) -> ScanCursor | None: ...

    def load_position(self, symbol: str, timeframe: str) -> PositionState | None: ...

    def commit(
        self,
        *,
        cursor: ScanCursor,
        position: PositionState | None,
        signal: TurtleSignal | None = None,
        decision_event: dict[str, Any] | None = None,
        outbox_record: dict[str, Any] | None = None,
    ) -> bool: ...


class InMemorySignalRepository:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], DetectorState] = {}
        self.signal_keys: set[str] = set()
        self.signals: list[TurtleSignal] = []
        self.cursors: dict[tuple[str, str], ScanCursor] = {}
        self.positions: dict[tuple[str, str], PositionState] = {}
        self.decision_events: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []

    def load_state(self, symbol: str, timeframe: str) -> DetectorState | None:
        return self.states.get((symbol, timeframe))

    def save_state(self, state: DetectorState) -> None:
        self.states[(state.symbol, state.timeframe)] = state

    def is_new(self, signal: TurtleSignal) -> bool:
        return signal_key(signal) not in self.signal_keys

    def save_signal(self, signal: TurtleSignal) -> None:
        self.signal_keys.add(signal_key(signal))
        self.signals.append(signal)

    def load_cursor(self, symbol: str, timeframe: str) -> ScanCursor | None:
        return self.cursors.get((symbol, timeframe))

    def load_position(self, symbol: str, timeframe: str) -> PositionState | None:
        return self.positions.get((symbol, timeframe))

    def commit(
        self,
        *,
        cursor: ScanCursor,
        position: PositionState | None,
        signal: TurtleSignal | None = None,
        decision_event: dict[str, Any] | None = None,
        outbox_record: dict[str, Any] | None = None,
    ) -> bool:
        """Apply an entire decision as one in-memory transaction.

        A duplicate business signal is a no-op; passive OBSERVE decisions still
        persist cursor/position updates when no signal is supplied.
        """
        if signal is not None and not self.is_new(signal):
            return False
        key = (cursor.symbol, cursor.timeframe)
        self.cursors[key] = cursor
        if position is not None:
            self.positions[key] = position
        if signal is not None:
            self.signal_keys.add(signal_key(signal))
            self.signals.append(signal)
        if decision_event is not None:
            self.decision_events.append(dict(decision_event))
        if outbox_record is not None:
            self.outbox.append(dict(outbox_record))
        return True


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

    def commit(
        self,
        *,
        cursor: ScanCursor,
        position: PositionState | None,
        signal: TurtleSignal | None = None,
        decision_event: dict[str, Any] | None = None,
        outbox_record: dict[str, Any] | None = None,
    ) -> bool:
        if signal is not None and not self.is_new(signal):
            return False
        # Mutate only after duplicate detection and write exactly one replacement
        # file, so no externally observable half-commit is possible.
        committed = InMemorySignalRepository.commit(
            self,
            cursor=cursor,
            position=position,
            signal=signal,
            decision_event=decision_event,
            outbox_record=outbox_record,
        )
        if committed:
            self._flush()
        return committed

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
        for raw in payload.get("cursors", []):
            cursor = ScanCursor(**raw)
            self.cursors[(cursor.symbol, cursor.timeframe)] = cursor
        for raw in payload.get("positions", []):
            values = dict(raw)
            identity = values.pop("identity")
            values["status"] = PositionStatus(values["status"])
            values["direction"] = Direction(values["direction"])
            position = PositionState(**values)
            key = tuple(identity)
            self.positions[(str(key[0]), str(key[1]))] = position
        self.decision_events.extend(payload.get("decision_events", []))
        self.outbox.extend(payload.get("outbox", []))

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        states = []
        for state in self.states.values():
            row = asdict(state)
            row["status"] = state.status.value
            row["direction"] = state.direction.value
            states.append(row)
        cursors = [asdict(value) for value in self.cursors.values()]
        positions = []
        for (symbol, timeframe), position in self.positions.items():
            row = asdict(position)
            row["status"] = position.status.value
            row["direction"] = position.direction.value
            row["identity"] = [symbol, timeframe]
            positions.append(row)
        payload = {
            "schema_version": "2",
            "states": states,
            "signal_keys": sorted(self.signal_keys),
            "cursors": cursors,
            "positions": positions,
            "decision_events": self.decision_events,
            "outbox": self.outbox,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


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
