"""Notification adapters receive signals only after repository deduplication."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Protocol

from ..models import TurtleSignal


class Notifier(Protocol):
    def notify(self, signal: TurtleSignal) -> None: ...


class ConsoleNotifier:
    def notify(self, signal: TurtleSignal) -> None:
        status = "可交易" if signal.tradeable else "仅记录"
        print(
            f"[{signal.signal_time}] {signal.symbol} {signal.timeframe} "
            f"{signal.direction.value} {signal.signal_type.value} "
            f"price={signal.trigger_price:.6g} ATR={signal.atr:.6g} "
            f"stop={signal.stop_price} next_add={signal.next_add_price} {status}"
        )


class JsonLinesNotifier:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def notify(self, signal: TurtleSignal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(signal.to_dict(), ensure_ascii=False) + "\n")


class StructuredLoggingNotifier:
    """Emit one machine-readable log record per deduplicated signal."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("turtle_detector.breakout_alert")

    def notify(self, signal: TurtleSignal) -> None:
        self.logger.info(
            json.dumps(signal.to_dict(), ensure_ascii=False, allow_nan=False)
        )


class CompositeNotifier:
    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self.notifiers = tuple(notifiers)

    def notify(self, signal: TurtleSignal) -> None:
        for notifier in self.notifiers:
            notifier.notify(signal)
