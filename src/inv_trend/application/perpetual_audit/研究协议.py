"""Frozen windows, sample provenance and an append-only experiment journal."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd


SAMPLE_STATES = {'DEVELOPMENT', 'UNSEEN_TEST', 'CONTAMINATED_TEST', 'PAPER_FORWARD'}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class Window:
    track: str
    stage: str
    start: str
    end: str
    sample_status: str
    fold: int = 0


def utc(value):
    return pd.Timestamp(value).tz_localize('UTC') if pd.Timestamp(value).tzinfo is None else pd.Timestamp(value).tz_convert('UTC')


def schedule(daily_index, execution_start=None, *, track='signal',
             development_end='2026-04-20', test_end='2026-09-06',
             sample_status='CONTAMINATED_TEST', unseen_evidence=None):
    """No shortening, no compressed missing dates, 440 prior observations excluded."""
    if sample_status not in SAMPLE_STATES:
        raise ValueError('unknown sample status')
    if sample_status == 'UNSEEN_TEST' and not unseen_evidence:
        raise ValueError('UNSEEN_TEST requires explicit provenance evidence')
    index = pd.DatetimeIndex(pd.to_datetime(daily_index, utc=True))
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError('daily calendar must be sorted and unique')
    if len(index) and (not (index == index.normalize()).all() or
                       not index.equals(pd.date_range(index[0], index[-1], freq='D'))):
        raise ValueError('daily calendar must contain every complete UTC day')
    if len(index) <= 440:
        return [], 'INSUFFICIENT_WARMUP'
    s = max(index[440], utc(execution_start).ceil('D') if execution_start else index[440])
    end = min(index[-1], utc(development_end))
    rows = []

    def add(stage, a, b, status='DEVELOPMENT', fold=0):
        if a <= b:
            rows.append(asdict(Window(track, stage, a.date().isoformat(),
                                      b.date().isoformat(), status, fold)))

    add('WARMUP', index[0], min(s - pd.Timedelta(days=1), index[-1]))
    if s > end:
        return rows, 'INSUFFICIENT_TRAINING'
    train_end = s + pd.Timedelta(days=729)
    add('INITIAL_TRAIN' if train_end <= end else 'INCOMPLETE_TRAIN', s, min(train_end, end))
    fold = 0
    cursor = train_end + pd.Timedelta(days=1)
    while cursor + pd.Timedelta(days=179) <= end:
        fold += 1
        add('TRAIN', s, cursor - pd.Timedelta(days=1), fold=fold)
        add('VALIDATION', cursor, cursor + pd.Timedelta(days=179), fold=fold)
        cursor += pd.Timedelta(days=180)
    add('DEVELOPMENT_TAIL', max(cursor, s), end)
    add('TEST', max(utc(development_end) + pd.Timedelta(days=1), s),
        min(index[-1], utc(test_end)), sample_status)
    return rows, 'READY' if fold >= 3 else 'INSUFFICIENT_FOLDS'


def candidates():
    return [{'architecture': a, 'pyramid': p, 'stop_n': 2., 'fast_entry': 20,
             'slow_entry': 55, 'fast_exit': 10, 'slow_exit': 20,
             'pyramid_step_n': .5, 'ma_period': 200, 'momentum_period': 365,
             'covariance_days': 60, 'role': 'CENTER'}
            for a in ('dual', 'daily', 'fast', 'slow', 'ma', 'momentum')
            for p in (False, True)]


def neighborhood(center):
    space = {'fast_entry': (16, 24), 'slow_entry': (44, 66),
             'fast_exit': (8, 12), 'slow_exit': (16, 24),
             'stop_n': (1.5, 2.5), 'covariance_days': (40, 90)}
    if center['pyramid']:
        space['pyramid_step_n'] = (1.,)
    if center['architecture'] == 'ma':
        space['ma_period'] = (160, 240)
    if center['architecture'] == 'momentum':
        space['momentum_period'] = (292, 438)
    return [dict(center, **{key: value}, role='DIAGNOSTIC_ONLY')
            for key, values in space.items() for value in values]


class ExperimentRegistry:
    """SQLite owns experiment state; JSONL is read-only migration input."""
    def __init__(self, path, *, root=None):
        from inv_trend.storage.实验登记 import SQLiteRegistry
        self.path = Path(path)
        self.backend = SQLiteRegistry(path, root=root)
        if self.path.exists():
            self.backend.import_jsonl(digest)

    def append(self, event):
        return self.backend.append(event, digest)

    def read(self):
        return self.backend.read()

    def start(self, spec, *, code_hash, data_hash, sample_status, parent=None, kind='RESEARCH'):
        if sample_status not in SAMPLE_STATES:
            raise ValueError('unknown sample status')
        run_id = uuid4().hex
        self.append(dict(event='START', run_id=run_id, spec=spec, spec_hash=digest(spec),
                         trial_id=digest([spec, code_hash, data_hash, sample_status]),
                         code_hash=code_hash, data_hash=data_hash, sample_status=sample_status,
                         parent=parent, kind=kind))
        return run_id

    def finish(self, run_id, status, **result):
        self.append(dict(event='FINISH', run_id=run_id, status=status, **result))

    def summary(self):
        starts = [r for r in self.read() if r['event'] == 'START']
        research = [r for r in starts if r.get('kind') not in {'DATA_AUDIT', 'REPRODUCTION'}]
        return dict(total_runs=len(starts), total_trials=len({r['trial_id'] for r in starts}),
                    statistical_trials=len({r['trial_id'] for r in research}),
                    data_audit_runs=sum(r.get('kind') == 'DATA_AUDIT' for r in starts),
                    unique_spec_count=len({r['spec_hash'] for r in starts}),
                    historical_search_count_known=False, trial_count_is_lower_bound=True)
