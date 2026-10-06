"""State transitions consume strategy facts, never calculate indicators or place orders."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .模型 import Event, EventKind, Phase, StateRecord


@dataclass(frozen=True)
class Facts:
    setup: bool
    candidate: bool
    close_confirmed: bool
    trend_allowed: bool
    execution_allowed: bool
    quality_allowed: bool
    fresh: bool
    direction: int = 0
    exit_triggered: bool = False
    exit_code: str = 'TREND_EXIT'
    # None means unknown; a confirmation never invents an actual position.
    position_open: bool | None = None
    position_ref: str | None = None
    position_direction: int | None = None
    position_event: str | None = None

    def __post_init__(self):
        for name in ('setup', 'candidate', 'close_confirmed', 'trend_allowed',
                     'execution_allowed', 'quality_allowed', 'fresh', 'exit_triggered'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be an explicit boolean')
        if type(self.direction) is not int or self.direction not in {-1, 0, 1}:
            raise ValueError('invalid fact direction')
        if self.position_open is not None and type(self.position_open) is not bool:
            raise ValueError('position knowledge must be true, false or unknown')
        if self.position_open is True and (not self.position_ref or
                (self.position_direction is None and self.direction == 0)):
            raise ValueError('an observed position needs reference and direction')
        if self.position_direction is not None and (type(self.position_direction) is not int or self.position_direction not in {-1, 1}):
            raise ValueError('invalid observed position direction')
        if self.position_event not in {None, 'ADD', 'REDUCE', 'STOP_UPDATE', 'TRAIL_STOP', 'POSITION_UPDATED'}:
            raise ValueError('unknown observed position event')
        if self.exit_code not in {'TREND_EXIT', 'STOP_EXIT', 'INVALIDATION_EXIT'}:
            raise ValueError('unknown exit code')


@dataclass(frozen=True)
class Transition:
    after: StateRecord
    event: Event | None
    reasons: tuple[str, ...]
    additional_events: tuple[Event, ...] = ()


def transition(before: StateRecord, facts: Facts) -> Transition:
    phase, direction, position_ref = before.phase, before.direction, before.position_ref
    event = None
    reasons = []
    position_direction = facts.position_direction if facts.position_direction is not None else facts.direction
    if facts.position_open is False and position_ref:
        phase, direction, position_ref = Phase.IDLE, 0, None
        event = Event(EventKind.POSITION_EVENT, 'POSITION_CLOSED')
    elif facts.position_open is True and not position_ref:
        phase, direction, position_ref = Phase.POSITION, position_direction, facts.position_ref
        event = Event(EventKind.POSITION_EVENT, 'POSITION_OBSERVED', details={'position_ref': position_ref})
    elif facts.position_open is True and facts.position_ref != position_ref:
        phase, direction, position_ref = Phase.POSITION, position_direction, facts.position_ref
        event = Event(EventKind.POSITION_EVENT, 'POSITION_UPDATED', details={'position_ref': position_ref})
    elif facts.position_open is True and facts.position_event:
        direction = position_direction
        event = Event(EventKind.POSITION_EVENT, facts.position_event, details={'position_ref': position_ref})

    gates = facts.quality_allowed and facts.fresh
    position_event = event
    if not facts.quality_allowed:
        reasons.append('QUALITY_GATE_BLOCKED')
    if not facts.fresh:
        reasons.append('STALE_DATA')
    if gates and position_ref:
        if facts.position_open is True and facts.exit_triggered and phase is not Phase.EXIT:
            phase = Phase.EXIT
            event = Event(EventKind.MARKET_SIGNAL, facts.exit_code)
        elif facts.position_open is None:
            reasons.append('POSITION_KNOWLEDGE_UNKNOWN')
    elif gates and event is None:
        if facts.close_confirmed and facts.trend_allowed and facts.execution_allowed and facts.direction:
            phase, direction = Phase.CONFIRMED_ENTRY, facts.direction
        elif (facts.candidate or facts.close_confirmed) and facts.direction:
            phase, direction = Phase.CANDIDATE, facts.direction
            if not facts.trend_allowed:
                reasons.append('TREND_GATE_BLOCKED')
            if not facts.execution_allowed and facts.close_confirmed:
                reasons.append('EXECUTION_GATE_BLOCKED')
        elif facts.setup:
            phase, direction = Phase.SETUP, facts.direction
        else:
            phase, direction = Phase.IDLE, 0
        if phase != before.phase or direction != before.direction:
            event = Event(EventKind.MARKET_SIGNAL, phase.value,
                          details={'direction': direction, 'previous': before.phase.value})

    after = replace(before, phase=phase, direction=direction, position_ref=position_ref,
                    revision=before.revision + 1)
    extra = (position_event,) if position_event is not None and position_event != event else ()
    return Transition(after, event, tuple(reasons), extra)
