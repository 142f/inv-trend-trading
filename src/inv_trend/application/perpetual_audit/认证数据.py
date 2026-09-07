"""Hash-checked research input packages; certification never inferred from prices."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import pandas as pd

from inv_trend.core.永续风控 import InstrumentSpec, PositionTier, specification_at
from inv_trend.data.永续历史 import inspect_frame


@dataclass
class CertifiedInputs:
    daily: dict
    minute: dict
    marks: dict
    funding: dict
    specs: list
    start: pd.Timestamp
    end: pd.Timestamp
    data_hash: str
    status: str
    provenance: dict


def load_certified(root):
    root = Path(root).resolve()
    path = root / '认证输入.json'
    if not path.exists():
        raise ValueError('缺少认证输入.json；API历史本身不证明资金费或规格完整')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 'perpetual-input-v1':
        raise ValueError('unsupported input schema')
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest()]

    def read(ref, kind):
        if not ref.get('source'):
            raise ValueError('every input requires source evidence')
        file = (root / ref['file']).resolve()
        if not file.is_relative_to(root):
            raise ValueError('input path escapes package')
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        if sha != ref['sha256']:
            raise ValueError(f'input hash mismatch: {file.name}')
        hashes.append(sha)
        if kind == 'specs':
            return json.loads(file.read_text(encoding='utf-8'))
        frame = pd.read_parquet(file) if file.suffix == '.parquet' else pd.read_csv(file)
        frame['timestamp'] = pd.to_datetime(frame.timestamp, utc=True)
        quality = inspect_frame(frame, frequency='D' if kind == 'daily' else 'min' if kind in ('minute', 'marks') else None)
        if quality['status'] != 'VALID':
            raise ValueError(f'invalid {kind}: {quality}')
        return frame.set_index('timestamp')

    inputs = {name: {} for name in ('daily', 'minute', 'marks', 'funding')}
    specs = []
    instruments = manifest.get('instruments', {})
    if set(instruments) != {'BTC-USDT-SWAP', 'ETH-USDT-SWAP'}:
        raise ValueError('BTC/ETH perpetual identity required')
    for symbol, refs in instruments.items():
        for kind in inputs:
            inputs[kind][symbol] = read(refs[kind], kind)
        funding_ref = refs['funding']
        if not funding_ref.get('settlement_schedule_evidence'):
            raise ValueError('funding completeness requires independent settlement schedule evidence')
        funding = inputs['funding'][symbol]
        if not {'funding_rate', 'mark_price_at_event'} <= set(funding):
            raise ValueError('funding lacks rate or mark at event')
        if funding.index.has_duplicates:
            raise ValueError('duplicate funding settlement events')
        for raw in read(refs['specs'], 'specs'):
            if raw['instrument'] != symbol:
                raise ValueError('spec identity mismatch')
            specs.append(InstrumentSpec(**dict(raw, tiers=tuple(PositionTier(**t) for t in raw['tiers']))))
    start = max([f.index[440] for f in inputs['daily'].values() if len(f) > 440] +
                [f.index[0] for k in ('minute', 'marks') for f in inputs[k].values()] +
                [pd.Timestamp(refs['funding']['coverage_start']) for refs in instruments.values()] +
                [min(pd.Timestamp(s.effective_from) for s in specs if s.instrument == symbol) for symbol in instruments])
    if any(len(f) <= 440 for f in inputs['daily'].values()):
        raise ValueError('insufficient warmup')
    end = min([f.index[-1] for k in ('minute', 'marks') for f in inputs[k].values()] +
              [f.index[-1] + pd.Timedelta(days=1) - pd.Timedelta(minutes=1) for f in inputs['daily'].values()] +
              [pd.Timestamp(refs['funding']['coverage_end']) for refs in instruments.values()])
    start = start.ceil('D')
    if end < start:
        raise ValueError('no common execution coverage')
    if end != end.normalize() + pd.Timedelta(hours=23, minutes=59):
        end = end.normalize() - pd.Timedelta(minutes=1)
    for symbol in instruments:
        # Every version boundary must be covered uniquely; endpoint checks alone
        # would miss a hole or overlap in the middle.
        boundaries = {start, end}
        for s in specs:
            if s.instrument == symbol:
                for value in (s.effective_from, s.effective_to):
                    if value and start <= pd.Timestamp(value) <= end:
                        boundaries.update((pd.Timestamp(value), pd.Timestamp(value) - pd.Timedelta(nanoseconds=1)))
        for t in boundaries:
            if start <= t <= end:
                specification_at(specs, symbol, t)
    status = 'VERIFIED_INPUTS' if all(s.spec_status == 'VERIFIED_HISTORICAL' for s in specs) else 'SIMULATED_SPEC'
    return CertifiedInputs(**inputs, specs=specs, start=start, end=end,
                           data_hash=hashlib.sha256(''.join(hashes).encode()).hexdigest(),
                           status=status, provenance=manifest)
