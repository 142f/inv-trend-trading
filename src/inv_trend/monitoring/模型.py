"""Typed, immutable monitoring evidence; no provider, execution or notification I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
import math
from numbers import Integral, Real
from types import MappingProxyType
from typing import Any, Mapping

import pandas as pd


class Phase(str, Enum):
    IDLE = 'IDLE'
    SETUP = 'SETUP'
    CANDIDATE = 'CANDIDATE'
    CONFIRMED_ENTRY = 'CONFIRMED_ENTRY'
    POSITION = 'POSITION'
    EXIT = 'EXIT'


class EventKind(str, Enum):
    MARKET_SIGNAL = 'MARKET_SIGNAL'
    POSITION_EVENT = 'POSITION_EVENT'
    DATA_HEALTH = 'DATA_HEALTH'
    SYSTEM_HEALTH = 'SYSTEM_HEALTH'
    NOTIFICATION_HEALTH = 'NOTIFICATION_HEALTH'


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        if not math.isfinite(value):
            raise ValueError('non-finite evidence cannot be persisted')
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('evidence timestamps must be timezone-aware')
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('evidence object keys must be strings')
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    raise TypeError(f'unsupported evidence type: {type(value).__name__}')


def canonical(value: Any) -> str:
    return json.dumps(json_value(value), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def frozen(value: Any) -> Any:
    value = json_value(value)
    if isinstance(value, dict):
        return MappingProxyType({key: frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    return value


def timeframe(value: str) -> str:
    normalized = {'1D': 'D1', '4H': 'H4'}.get(value.upper(), value.upper())
    if normalized not in {'D1', 'H4'}:
        raise ValueError('monitoring timeframe must be D1/1D or H4/4H')
    return normalized


def timestamp(value: str) -> str:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('monitoring timestamp must be timezone-aware')
    return parsed.isoformat()


@dataclass(frozen=True)
class Event:
    kind: EventKind
    code: str
    severity: str = 'INFO'
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.kind, EventKind) or not self.code:
            raise ValueError('typed event kind and code are required')
        if self.severity not in {'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
            raise ValueError('invalid event severity')
        object.__setattr__(self, 'details', frozen(self.details))

    def to_dict(self):
        return {'kind': self.kind.value, 'code': self.code, 'severity': self.severity,
                'details': json_value(self.details)}


@dataclass(frozen=True)
class StateRecord:
    symbol: str
    timeframe: str
    phase: Phase = Phase.IDLE
    direction: int = 0
    position_ref: str | None = None
    revision: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.symbol or not isinstance(self.phase, Phase):
            raise ValueError('state identity and typed phase are required')
        if type(self.direction) is not int or self.direction not in {-1, 0, 1}:
            raise ValueError('invalid state direction')
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError('invalid state revision')
        if self.phase in {Phase.POSITION, Phase.EXIT} and not self.position_ref:
            raise ValueError('position/exit states require an observed position reference')
        object.__setattr__(self, 'symbol', self.symbol.upper())
        object.__setattr__(self, 'timeframe', timeframe(self.timeframe))
        object.__setattr__(self, 'details', frozen(self.details))

    def to_dict(self):
        return {'symbol': self.symbol, 'timeframe': self.timeframe, 'phase': self.phase.value,
                'direction': self.direction, 'position_ref': self.position_ref,
                'revision': self.revision, 'details': json_value(self.details)}

    @classmethod
    def from_dict(cls, value):
        return cls(**(dict(value) | {'phase': Phase(value['phase'])}))


@dataclass(frozen=True)
class DecisionSnapshot:
    instrument_id: str
    symbol: str
    timeframe: str
    bar_time: str | None
    evaluated_at: str
    dataset_version: str | None
    strategy_version: str
    config_hash: str
    git_commit: str
    code_hash: str
    market: Mapping[str, Any] | None
    indicators: Mapping[str, Any]
    conditions: Mapping[str, bool]
    state_before: Phase
    state_after: Phase
    reasons: tuple[str, ...]
    event: Event | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    state_context: Mapping[str, Any] = field(default_factory=dict)
    additional_events: tuple[Event, ...] = ()

    def __post_init__(self):
        if not all((self.instrument_id, self.symbol, self.strategy_version, self.git_commit)):
            raise ValueError('snapshot identity and code provenance are required')
        for value in (self.config_hash, self.code_hash):
            if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('snapshot config/code hash must be SHA256')
        if not isinstance(self.state_before, Phase) or not isinstance(self.state_after, Phase):
            raise ValueError('snapshot states must be typed')
        if any(type(value) is not bool for value in self.conditions.values()):
            raise ValueError('unknown conditions must explicitly fail closed')
        events = tuple(self.additional_events)
        if any(not isinstance(event, Event) or event.kind is EventKind.MARKET_SIGNAL for event in events):
            raise ValueError('additional events must be typed position or health observations')
        all_events = ((self.event,) if self.event else ()) + events
        if len({canonical(event.to_dict()) for event in all_events}) != len(all_events):
            raise ValueError('duplicate events in one decision')
        object.__setattr__(self, 'additional_events', events)
        confirming = (self.state_after is Phase.CONFIRMED_ENTRY and self.state_before is not Phase.CONFIRMED_ENTRY)
        if confirming or (self.event and self.event.kind is EventKind.MARKET_SIGNAL):
            if not self.dataset_version or not self.bar_time or self.market is None:
                raise ValueError('market signals require versioned market evidence')
            if self.conditions.get('quality_gate') is not True or self.conditions.get('freshness_gate') is not True:
                raise ValueError('market signals require quality and freshness gates')
            required = {'open', 'high', 'low', 'close', 'volume'}
            if not required.issubset(self.market):
                raise ValueError('market signals require complete OHLCV evidence')
            prices = [self.market[name] for name in ('open', 'high', 'low', 'close')]
            if any(isinstance(value, bool) or not isinstance(value, Real)
                   or not math.isfinite(value) or value <= 0 for value in prices):
                raise ValueError('market signals require finite positive prices')
            opening, high, low, close = prices
            if low > min(opening, close) or high < max(opening, close) or low > high:
                raise ValueError('market OHLC price relationships are invalid')
            volume = self.market['volume']
            if volume is not None and (isinstance(volume, bool) or not isinstance(volume, Real)
                                       or not math.isfinite(volume) or volume < 0):
                raise ValueError('market volume must be known nonnegative or explicitly unavailable')
        if confirming or (self.event and self.event.code == 'CONFIRMED_ENTRY'):
            if not self.dataset_version or not self.bar_time or self.market is None:
                raise ValueError('confirmed states require versioned evidence')
            if any(
                self.conditions.get(name) is not True
                for name in ('quality_gate', 'freshness_gate', 'close_confirmation', 'trend_gate', 'execution_gate')
            ):
                raise ValueError('confirmed entries require every final gate')
        object.__setattr__(self, 'symbol', self.symbol.upper())
        object.__setattr__(self, 'timeframe', timeframe(self.timeframe))
        object.__setattr__(self, 'evaluated_at', timestamp(self.evaluated_at))
        if self.bar_time is not None:
            object.__setattr__(self, 'bar_time', timestamp(self.bar_time))
        for name in ('market', 'indicators', 'conditions', 'context', 'state_context'):
            object.__setattr__(self, name, frozen(getattr(self, name)))
        object.__setattr__(self, 'reasons', tuple(self.reasons))

    def to_dict(self):
        return {name: json_value(getattr(self, name)) for name in (
            'instrument_id', 'symbol', 'timeframe', 'bar_time', 'evaluated_at',
            'dataset_version', 'strategy_version', 'config_hash', 'git_commit',
            'code_hash', 'market', 'indicators', 'conditions', 'state_before',
            'state_after', 'reasons', 'context', 'state_context',
        )} | {'event': self.event.to_dict() if self.event else None,
              'additional_events': [event.to_dict() for event in self.additional_events], 'schema_version': 3}

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        if value.pop('schema_version', None) != 3:
            raise ValueError('unsupported decision snapshot schema')
        recorded_id, recorded_key = value.pop('decision_id', None), value.pop('input_key', None)
        if value['event'] is not None:
            event = value['event']
            value['event'] = Event(**(event | {'kind': EventKind(event['kind'])}))
        value['additional_events'] = tuple(Event(**(event | {'kind': EventKind(event['kind'])}))
                                           for event in value['additional_events'])
        value['state_before'], value['state_after'] = Phase(value['state_before']), Phase(value['state_after'])
        snapshot = cls(**value)
        if recorded_id is not None and recorded_id != snapshot.decision_id:
            raise ValueError('decision snapshot content hash does not match')
        if recorded_key is not None and recorded_key != snapshot.input_key:
            raise ValueError('decision snapshot input hash does not match')
        return snapshot

    @property
    def input_key(self):
        payload = self.to_dict()
        # Changes of mutable state or clock alone cannot rescan identical inputs.
        payload['data_gates'] = {key: self.conditions.get(key)
                                 for key in ('quality_gate', 'freshness_gate')}
        for key in ('evaluated_at', 'state_before', 'state_after', 'event', 'reasons',
                    'state_context', 'conditions', 'indicators', 'additional_events'):
            payload.pop(key)
        return digest(payload)

    @property
    def decision_id(self):
        payload = self.to_dict()
        payload.pop('evaluated_at')
        return digest(payload)
