"""Runtime-only settings; strategy parameters use the existing shared loader."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from .模型 import digest, timeframe


@dataclass(frozen=True)
class Target:
    symbol: str
    timeframe: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError('monitoring target symbol is required')
        object.__setattr__(self, 'symbol', self.symbol.upper())
        object.__setattr__(self, 'timeframe', timeframe(self.timeframe))

    @property
    def key(self):
        return self.symbol + ':' + self.timeframe


@dataclass(frozen=True)
class RuntimeConfig:
    data_root: Path = Path('data')
    runtime_dir: Path = Path('outputs/趋势监控')
    strategy_config: Path | None = None
    symbols: tuple[str, ...] = ()  # Empty resolves all ACTIVE Repository instruments.
    timeframes: tuple[str, ...] = ('D1', 'H4')
    refresh: bool = True
    interval_seconds: float = 300.
    poll_seconds: float = 5.
    lock_timeout_seconds: float = 0.
    scan_timeout_seconds: float = 120.
    heartbeat_timeout_seconds: float = 180.
    workers: int = 2
    consecutive_failure_threshold: int = 3
    provider_timeout_seconds: int = 10
    provider_retries: int = 1
    paper_equity: float | None = None
    notifications: Mapping[str, bool] = field(default_factory=dict)
    supervision: Mapping[str, float | int] = field(default_factory=dict)

    def __post_init__(self):
        if (not isinstance(self.notifications, Mapping) or
                set(self.notifications) - {'setup', 'candidate', 'confirmed_entry', 'exit', 'idle', 'position'} or
                any(type(value) is not bool for value in self.notifications.values())):
            raise ValueError('notifications must contain supported boolean state controls')
        object.__setattr__(self, 'notifications', MappingProxyType(dict(self.notifications)))
        allowed = {'startup_timeout_seconds', 'hard_scan_timeout_seconds', 'restart_delay_seconds',
                   'graceful_stop_seconds', 'max_restarts', 'restart_window_seconds'}
        if not isinstance(self.supervision, Mapping) or set(self.supervision) - allowed:
            raise ValueError('unsupported supervision settings')
        for name, value in self.supervision.items():
            if name == 'max_restarts':
                if type(value) is not int or value < 0:
                    raise ValueError('supervisor restart budget must be a nonnegative integer')
            elif isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('supervisor timing must be finite and positive')
        object.__setattr__(self, 'supervision', MappingProxyType(dict(self.supervision)))
        for name in ('interval_seconds', 'poll_seconds', 'scan_timeout_seconds', 'heartbeat_timeout_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if self.poll_seconds > self.interval_seconds:
            raise ValueError('poll interval exceeds scan interval')
        if (isinstance(self.lock_timeout_seconds, bool) or not math.isfinite(self.lock_timeout_seconds)
                or self.lock_timeout_seconds < 0):
            raise ValueError('lock timeout must be nonnegative')
        for name in ('workers', 'provider_timeout_seconds', 'provider_retries', 'consecutive_failure_threshold'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if self.workers > 32:
            raise ValueError('monitoring workers exceed supported first-version limit')
        if type(self.refresh) is not bool:
            raise ValueError('refresh must be an explicit boolean')
        if self.paper_equity is not None and (isinstance(self.paper_equity, bool) or
                not math.isfinite(self.paper_equity) or self.paper_equity <= 0):
            raise ValueError('paper equity must be explicitly positive')
        for name in ('symbols', 'timeframes'):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)) or any(
                    not isinstance(item, str) or not item.strip() or item != item.strip() for item in value):
                raise ValueError(f'{name} must be a list of nonempty strings')
        symbols = tuple(symbol.upper() for symbol in self.symbols)
        frames = tuple(timeframe(frame) for frame in self.timeframes)
        if len(set(symbols)) != len(symbols) or not frames or len(set(frames)) != len(frames):
            raise ValueError('duplicate symbols or empty/duplicate timeframes')
        object.__setattr__(self, 'symbols', symbols)
        object.__setattr__(self, 'timeframes', frames)
        for name in ('data_root', 'runtime_dir', 'strategy_config'):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).resolve())

    def targets(self, repository):
        symbols = self.symbols or tuple(sorted(symbol for symbol, item in repository.instruments.items()
                                               if item.status == 'ACTIVE'))
        result = []
        for symbol in symbols:
            if repository.instrument(symbol).status != 'ACTIVE':
                raise ValueError(f'reserved instrument cannot be monitored: {symbol}')
            result.extend(Target(symbol, frame) for frame in self.timeframes)
        if not result:
            raise ValueError('monitoring target universe is empty')
        return tuple(result)

    def to_dict(self):
        return {item.name: str(value) if isinstance(value, Path) else dict(value) if isinstance(value, Mapping) else value
                for item in fields(self) for value in (getattr(self, item.name),)}

    @property
    def config_hash(self):
        return digest(self.to_dict())


def load_runtime_config(path: str | Path):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('monitoring configuration must be an object')
    unknown = set(raw) - {item.name for item in fields(RuntimeConfig)}
    if unknown:
        raise ValueError(f'unsupported monitoring configuration keys: {sorted(unknown)}')
    for name in ('data_root', 'runtime_dir', 'strategy_config'):
        if raw.get(name) is not None:
            raw[name] = path.parent / raw[name]
    return RuntimeConfig(**raw)
