"""Repository-only scanning, shared strategy calculation and durable decisions."""
from __future__ import annotations

from inv_trend.data.integrity import sha256_file

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import math
from pathlib import Path
import sqlite3
import subprocess
from typing import Protocol

import numpy as np
import pandas as pd

from inv_trend.data.models import (CatalogAccessError, CatalogBackendConflictError, DataCertificationError,
                                   DataLineageError, DataQualityError, DataUnavailableError, InstrumentConfig)
from inv_trend.observability.脱敏 import redact

from .事件存储 import EventStore
from .模型 import DecisionSnapshot, Event, EventKind, Phase, StateRecord, digest, timeframe
from .新鲜度 import MonitoringFreshnessPolicy
from .状态机 import Facts, transition
from .策略适配 import ReferenceAccount, TurtleEvaluator, evidence


class MarketRepository(Protocol):
    def instrument(self, symbol: str) -> InstrumentConfig: ...
    def load_bars(self, symbol: str, timeframe: str, **kwargs) -> pd.DataFrame: ...
    def load_bars_version(self, symbol: str, timeframe: str, dataset_version: str, **kwargs) -> pd.DataFrame: ...


@dataclass(frozen=True)
class CodeVersion:
    git_commit: str
    source_hash: str

    @classmethod
    def capture(cls, root: str | Path | None = None):
        root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
        commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, check=True).stdout.strip()
        sources = sorted(list((root / 'src').rglob('*.py')) + [root / 'pyproject.toml'])
        content = {path.relative_to(root).as_posix(): sha256_file(path)
                   for path in sources}
        return cls(commit, digest(content))


def _indexed(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if 'timestamp' in result:
        result.index = pd.DatetimeIndex(pd.to_datetime(result['timestamp'], utc=True))
    if not isinstance(result.index, pd.DatetimeIndex) or result.index.tz is None:
        raise DataQualityError('monitoring needs timezone-aware canonical bar timestamps')
    if result.index.hasnans or result.index.has_duplicates or not result.index.is_monotonic_increasing:
        raise DataQualityError('monitoring bar timestamps must be unique and chronological')
    if 'is_complete' not in result or not result['is_complete'].map(
        lambda value: isinstance(value, (bool, np.bool_)) and bool(value)).all():
        raise DataQualityError('formal Repository series must contain only explicit complete bars')
    if not {'open', 'high', 'low', 'close', 'volume', 'bar_end'}.issubset(result.columns):
        raise DataQualityError('canonical OHLCV and bar_end are required')
    prices = result[['open', 'high', 'low', 'close']]
    if prices.map(lambda value: isinstance(value, (bool, np.bool_))).any().any():
        raise DataQualityError('boolean prices are invalid')
    numeric = prices.apply(pd.to_numeric, errors='coerce')
    if not np.isfinite(numeric.to_numpy(dtype=float)).all() or (numeric <= 0).any().any():
        raise DataQualityError('canonical prices must be finite and positive')
    if ((numeric['low'] > numeric[['open', 'close']].min(axis=1)) |
            (numeric['high'] < numeric[['open', 'close']].max(axis=1)) |
            (numeric['low'] > numeric['high'])).any():
        raise DataQualityError('canonical OHLC relationships are invalid')
    volume = pd.to_numeric(result['volume'], errors='coerce')
    supplied = result['volume'].notna()
    if ((supplied & (~np.isfinite(volume) | (volume < 0))).any() or
            result['volume'].map(lambda value: isinstance(value, (bool, np.bool_))).any()):
        raise DataQualityError('canonical volume must be nonnegative or absent')
    ends = pd.DatetimeIndex(pd.to_datetime(result['bar_end']))
    if ends.tz is None or ends.hasnans or (ends <= result.index).any():
        raise DataQualityError('canonical bar ends must be aware and later than bar starts')
    return result


def _frame_digest(frame):
    return digest({'columns': [(str(key), str(value)) for key, value in frame.dtypes.items()],
                   'content': hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()).hexdigest()})


def _health_generation(before, health):
    previous = before.details.get('data_health_generation', 0)
    if type(previous) is not int or previous < 0:
        raise ValueError('monitoring data-health generation is invalid')
    return previous + int(before.details.get('data_health') != health)


class MonitorScanner:
    def __init__(self, repository: MarketRepository, store: EventStore, evaluator: TurtleEvaluator,
                 *, code_version: CodeVersion | None = None, freshness=None, account_max_age_seconds=300.):
        if not math.isfinite(account_max_age_seconds) or account_max_age_seconds <= 0:
            raise ValueError('reference account age must be positive')
        self.repository, self.store, self.evaluator = repository, store, evaluator
        self.code = code_version or CodeVersion.capture()
        self.freshness = freshness or MonitoringFreshnessPolicy()
        self.account_max_age_seconds = account_max_age_seconds
        self.config_hash = digest({'rules': evidence(evaluator.engine.rules),
                                  'specs': {name: evidence(spec) for name, spec in sorted(evaluator.engine.specs.items())},
                                  'proximity_n': evaluator.proximity_n,
                                  'freshness_grace_seconds': self.freshness.grace_seconds,
                                  'account_max_age_seconds': account_max_age_seconds})

    def scan(self, symbol: str, frame: str, *, now: datetime, account: ReferenceAccount | None = None, guard=None):
        if self.store.purpose != 'live':
            raise ValueError('live scanning requires a live event store')
        symbol, frame = symbol.upper(), timeframe(frame)
        instrument = self.repository.instrument(symbol)
        try:
            bars = self.repository.load_bars(symbol, frame, adjusted=False)
        except (DataLineageError, DataQualityError, FileNotFoundError, sqlite3.DatabaseError) as exc:
            return self.record_failure(instrument, frame, now=now, error=exc)
        try:
            return self._scan_verified_prefix(bars, instrument, frame, now=now,
                                              account=account or ReferenceAccount(), mode='live', guard=guard)
        except (DataLineageError, DataQualityError) as exc:
            return self.record_failure(instrument, frame, now=now, error=exc)

    def record_failure(self, instrument, frame, *, now, error, category=None, kind=EventKind.DATA_HEALTH):
        if kind not in {EventKind.DATA_HEALTH, EventKind.SYSTEM_HEALTH, EventKind.NOTIFICATION_HEALTH}:
            raise ValueError('failure records require a typed health event')
        before = self.store.state(instrument.symbol, frame)
        if category is None:
            category = ('DATA_MISSING' if isinstance(error, (DataUnavailableError, FileNotFoundError)) else
                        'QUALITY_FAILURE' if isinstance(error, (DataQualityError, DataCertificationError)) else
                        'CATALOG_FAILURE' if isinstance(error, (sqlite3.DatabaseError, CatalogAccessError, CatalogBackendConflictError)) else
                        'DATA_MISSING' if isinstance(error, FileNotFoundError) else 'MANIFEST_FAILURE')
        condition = {'quality_gate': False, 'freshness_gate': False}
        details = {'category': category, 'error_type': type(error).__name__, 'message': redact(str(error))}
        generation = _health_generation(before, category)
        previous_health = before.details.get('data_health')
        event = Event(kind, category, 'ERROR', details) if previous_health != category else None
        after = replace(before, revision=before.revision + 1,
                        details=dict(before.details) | {'data_health': category, 'data_health_generation': generation})
        snapshot = DecisionSnapshot(
            instrument.instrument_id, instrument.symbol, frame, None, now.isoformat(), None,
            self.evaluator.strategy_version, self.config_hash, self.code.git_commit, self.code.source_hash,
            None, {}, condition, before.phase, after.phase, (category,), event,
            context={'mode': 'live', 'failure': details, 'data_health_generation': generation},
            state_context={'before': before.to_dict()},
        )
        return self.store.commit(snapshot, before, after)

    def _scan_verified_prefix(self, bars, instrument, frame, *, now, account, mode, guard=None):
        """Internal replay boundary: bars derive from one verified Repository load."""
        before = self.store.state(instrument.symbol, frame)
        bars, probe, known, fresh_result = self._prepare_verified_prefix(
            bars, instrument, frame, now=now, account=account, mode=mode, before=before)
        if guard is not None:
            guard()
        existing = self.store.decision_for_input(probe.input_key)
        if existing is not None:
            return {'created': False, 'snapshot': existing, 'skipped': True}
        snapshot, after = self._evaluate_decision(bars, instrument, frame, before,
                                                 probe, known, fresh_result)
        return self.store.commit(snapshot, before, after, enqueue=mode == 'live', guard=guard)

    def _prepare_verified_prefix(self, bars, instrument, frame, *, now, account, mode, before):
        if mode not in {'live', 'replay'}:
            raise ValueError('unknown monitoring evaluation mode')
        bars = _indexed(bars)
        if bars.empty:
            raise DataQualityError('no completed bars for monitoring')
        version = str(bars.attrs.get('dataset_version') or '')
        contract = bars.attrs.get('quality_contract')
        if not version or not isinstance(contract, dict):
            raise DataLineageError('Repository dataset version or quality contract is missing')
        if (contract.get('instrument_id') != instrument.instrument_id or contract.get('dataset_version') != version
                or contract.get('symbol') != instrument.symbol or contract.get('timeframe') != frame
                or bars.attrs.get('instrument_id') != instrument.instrument_id
                or bars.attrs.get('symbol') != instrument.symbol or bars.attrs.get('timeframe') != frame):
            raise DataLineageError('Repository quality contract identity is misbound')
        for name, expected in {'instrument_id': instrument.instrument_id, 'symbol': instrument.symbol,
                               'timeframe': frame, 'dataset_version': version}.items():
            if name not in bars or not bars[name].eq(expected).all():
                raise DataLineageError(f'Repository bar {name} is misbound')
        quality = (contract.get('backtest_suitable') is True and contract.get('quality_status') == 'CURATED'
                   and contract.get('missing_intervals') == [])
        fresh_result = self.freshness.evaluate(instrument, frame, bars.index[-1], now=now)
        fresh = fresh_result.is_fresh and bool((pd.to_datetime(bars['bar_end']) <= pd.Timestamp(now)).all())
        known = account.is_known(now, self.account_max_age_seconds)
        health = 'FRESH' if quality and fresh else 'QUALITY_FAILURE' if not quality else 'STALE_DATA'
        context = {'mode': mode, 'prefix_hash': _frame_digest(bars),
                   'quality_contract_hash': digest(contract), 'account': account.to_dict(),
                   'account_observation_known': known, 'instrument': evidence(instrument),
                   'account_max_age_seconds': self.account_max_age_seconds,
                   'data_health_generation': _health_generation(before, health)}
        market = {name: evidence(bars.iloc[-1].get(name)) for name in ('open', 'high', 'low', 'close', 'volume')}
        probe = DecisionSnapshot(
            instrument.instrument_id, instrument.symbol, frame, bars.index[-1].isoformat(), now.isoformat(), version,
            self.evaluator.strategy_version, self.config_hash, self.code.git_commit, self.code.source_hash,
            market, {}, {'quality_gate': quality, 'freshness_gate': fresh},
            before.phase, before.phase, (), context=context,
        )
        return bars, probe, known, fresh_result

    def _evaluate_decision(self, bars, instrument, frame, before, probe, known, fresh_result):
        # Calculate from the exact frozen evidence already bound to this input,
        # never a mutable portfolio that an account observer can update in flight.
        account = ReferenceAccount.from_dict(probe.context['account'])
        quality, fresh = probe.conditions['quality_gate'], probe.conditions['freshness_gate']
        observation = self.evaluator.evaluate(bars, instrument.symbol, frame, account,
                                             account_known=known, pending=before.details.get('candidate'))
        position = account.portfolio.positions.get(instrument.symbol) if known else None
        current_position = evidence(asdict(position)) if position else None
        prior_position = before.details.get('position_observation')
        position_event = None
        if position and prior_position:
            prior_qty = sum(unit['qty'] for unit in prior_position['units'])
            if position.side != prior_position['side']:
                position_event = 'POSITION_UPDATED'
            elif position.total_qty > prior_qty:
                position_event = 'ADD'
            elif position.total_qty < prior_qty:
                position_event = 'REDUCE'
            elif position.stop_price != prior_position['stop_price']:
                position_event = 'STOP_UPDATE'
        facts = Facts(observation.setup, observation.candidate, observation.close_confirmed,
                      observation.conditions['trend_gate'], observation.execution_allowed, quality, fresh,
                      observation.direction, observation.exit_triggered, observation.exit_code,
                      position is not None if known else None,
                      account.position_refs.get(instrument.symbol) if position else None,
                      position.side if position else None, position_event)
        outcome = transition(before, facts)
        health = 'FRESH' if quality and fresh else 'QUALITY_FAILURE' if not quality else 'STALE_DATA'
        details = dict(outcome.after.details) | {'candidate': dict(observation.pending) if outcome.after.phase is Phase.CANDIDATE else {},
                                               'data_health': health,
                                               'data_health_generation': probe.context['data_health_generation']}
        if known:
            details['position_observation'] = current_position
        after = replace(outcome.after, details=details)
        event = outcome.event
        additional = outcome.additional_events
        if before.details.get('data_health') != health:
            health_event = Event(EventKind.DATA_HEALTH,
                                 ('DATA_READY' if before.details.get('data_health') is None else 'DATA_RECOVERED')
                                 if health == 'FRESH' else health,
                                 'INFO' if health == 'FRESH' else 'ERROR', fresh_result.to_dict())
            if event is None:
                event = health_event
            else:
                additional += (health_event,)
        conditions = dict(observation.conditions) | {'quality_gate': quality, 'freshness_gate': fresh}
        snapshot = replace(probe, indicators=observation.indicators, conditions=conditions,
                           state_after=after.phase, reasons=tuple(outcome.reasons) + observation.reasons,
                           event=event, additional_events=additional, state_context={'before': before.to_dict(),
                           'strategy_after': observation.strategy_state, 'intents': observation.intents})
        return snapshot, after

    def reproduce(self, saved: dict) -> dict:
        """Recalculate recorded evidence without reading current or changing state."""
        original = DecisionSnapshot.from_dict(saved)
        if (original.code_hash, original.git_commit, original.config_hash, original.strategy_version) != (
                self.code.source_hash, self.code.git_commit, self.config_hash, self.evaluator.strategy_version):
            raise DataLineageError('reproduction requires the recorded code and configuration')
        if original.bar_time is None or original.dataset_version is None:
            raise ValueError('repository failure records have no market decision to recalculate')
        bars = self.repository.load_bars_version(original.symbol, original.timeframe,
                                                original.dataset_version, adjusted=False)
        bars = _indexed(bars)
        target = pd.Timestamp(original.bar_time)
        if target not in bars.index:
            raise DataLineageError('recorded bar is absent from the immutable version')
        prefix = bars.loc[:target].copy()
        before = StateRecord.from_dict(original.state_context['before'])
        if (before.symbol, before.timeframe, before.phase) != (
                original.symbol, original.timeframe, original.state_before):
            raise DataLineageError('recorded prior state is misbound')
        account = ReferenceAccount.from_dict(original.context['account'])
        prefix, probe, known, fresh_result = self._prepare_verified_prefix(
            prefix, self.repository.instrument(original.symbol), original.timeframe,
            now=pd.Timestamp(original.evaluated_at), account=account,
            mode=original.context['mode'], before=before)
        if probe.input_key != original.input_key:
            raise DataLineageError('recorded Repository or reference inputs no longer match')
        result, _ = self._evaluate_decision(prefix, self.repository.instrument(original.symbol),
                                           original.timeframe, before, probe, known, fresh_result)
        if result.decision_id != original.decision_id:
            raise DataLineageError('recorded decision is not reproduced by the bound inputs')
        return result.to_dict() | {'decision_id': result.decision_id, 'input_key': result.input_key}
