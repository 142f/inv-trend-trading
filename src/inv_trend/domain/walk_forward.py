"""Canonical ordering rules for causal bar events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class BarEvent:
    when: Any
    phase: str
    symbol: str
    label: Any

    def __post_init__(self) -> None:
        if self.phase not in {"open", "close"}:
            raise ValueError("bar event phase must be open or close")


def order_bar_events(events: Iterable[BarEvent], *, explicit_clock: bool) -> tuple[BarEvent, ...]:
    """Order close before a simultaneous next open when timestamps are explicit."""

    def key(event: BarEvent) -> tuple[Any, int, str]:
        if explicit_clock:
            priority = 0 if event.phase == "close" else 1
        else:
            priority = 0 if event.phase == "open" else 1
        return event.when, priority, event.symbol

    return tuple(sorted(events, key=key))


__all__ = ["BarEvent", "order_bar_events"]
