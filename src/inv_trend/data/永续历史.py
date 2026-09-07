"""Public OKX history acquisition with raw-page lineage and explicit coverage gaps."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


STREAMS = {
    'daily': ('/api/v5/market/history-candles', {'bar': '1Dutc'}, 'D'),
    'minute': ('/api/v5/market/history-candles', {'bar': '1m'}, 'min'),
    'mark_price': ('/api/v5/market/history-mark-price-candles', {'bar': '1m'}, 'min'),
    'funding': ('/api/v5/public/funding-rate-history', {}, None),
}


class OKXHistory:
    def __init__(self, root, *, transport=None, timeout=15):
        self.root = Path(root)
        self.transport = transport
        self.timeout = timeout

    def request(self, endpoint, params):
        if self.transport:
            result = self.transport(endpoint, params)
        else:
            url = 'https://www.okx.com' + endpoint + '?' + urlencode(params)
            with urlopen(Request(url, headers={'User-Agent': 'inv-trend-audit/1'}), timeout=self.timeout) as response:
                result = json.load(response)
        if result.get('code') != '0' or not isinstance(result.get('data'), list):
            raise ValueError(f"OKX response failure: {result.get('code')} {result.get('msg')}")
        return result

    def download(self, instrument, stream, *, end='2026-09-07', max_pages=None):
        """Reverse-page until exhaustion; an imposed cap never certifies inception."""
        if instrument not in {'BTC-USDT-SWAP', 'ETH-USDT-SWAP'}:
            raise ValueError('only registered linear perpetual instruments allowed')
        endpoint, extra, _ = STREAMS[stream]
        cutoff = int(pd.Timestamp(end, tz='UTC').timestamp() * 1000)
        cursor, pages, exhausted = cutoff, 0, False
        directory = self.root / instrument / stream
        directory.mkdir(parents=True, exist_ok=True)
        run = str(time.time_ns())
        records, hashes = [], []
        try:
            while max_pages is None or pages < max_pages:
                payload = self.request(endpoint, dict(instId=instrument, after=str(cursor), limit='100', **extra))
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
                sha = hashlib.sha256(raw).hexdigest()
                (directory / f'{run}-{pages:06d}-{sha[:12]}.json').write_bytes(raw)
                hashes.append(sha)
                batch = payload['data']
                if not batch:
                    exhausted = True
                    break
                stamps = [int(x['fundingTime'] if stream == 'funding' else x[0]) for x in batch]
                if min(stamps) >= cursor:
                    raise ValueError('history cursor did not move backward')
                for stamp, item in zip(stamps, batch):
                    if stamp >= cutoff:
                        continue
                    if stream == 'funding':
                        rate = item.get('realizedRate')
                        if rate in (None, ''):
                            rate = item.get('fundingRate')
                        records.append(dict(timestamp=pd.Timestamp(stamp, unit='ms', tz='UTC'),
                                            funding_rate=float(rate)))
                    elif str(item[-1]) == '1':
                        records.append(dict(timestamp=pd.Timestamp(stamp, unit='ms', tz='UTC'),
                                            **dict(zip(('open', 'high', 'low', 'close'), map(float, item[1:5]))),
                                            volume=float(item[6]) if stream != 'mark_price' and len(item) >= 9 else 0.,
                                            volume_contracts=float(item[5]) if stream != 'mark_price' else 0.))
                cursor = min(stamps)
                pages += 1
                if not self.transport:
                    time.sleep(.12)
            status, error = ('ENDPOINT_EXHAUSTED' if exhausted else 'TRUNCATED'), None
        except Exception as exc:
            status, error = 'DOWNLOAD_ERROR', f'{type(exc).__name__}: {exc}'
        frame = pd.DataFrame(records)
        if not frame.empty:
            # Conflicting overlap is not silently overwritten.
            for _, group in frame[frame.timestamp.duplicated(False)].groupby('timestamp'):
                if len(group.drop_duplicates()) > 1:
                    raise ValueError('conflicting history overlap')
            frame = frame.drop_duplicates('timestamp').sort_values('timestamp')
            frame.to_parquet(directory / f'{run}-行情.parquet', index=False)
        manifest = dict(instrument=instrument, stream=stream, endpoint=endpoint,
                        source='OKX_PUBLIC_API', status=status, error=error, pages=pages,
                        raw_sha256=hashes, first_ts=str(frame.timestamp.iloc[0]) if len(frame) else None,
                        last_ts=str(frame.timestamp.iloc[-1]) if len(frame) else None,
                        observations=len(frame), inception_verified=False,
                        funding_schedule_verified=False,
                        file=f'{run}-行情.parquet' if len(frame) else None)
        (directory / f'{run}-采集清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        return frame, manifest


def inspect_frame(frame, *, frequency=None):
    if frame.empty:
        return dict(first_ts=None, last_ts=None, observations=0, gaps=None, status='MISSING')
    idx = pd.DatetimeIndex(pd.to_datetime(frame['timestamp'], utc=True))
    if idx.has_duplicates or not idx.is_monotonic_increasing:
        return dict(first_ts=str(idx.min()), last_ts=str(idx.max()), observations=len(idx),
                    gaps=None, status='INVALID_CALENDAR')
    gaps = len(pd.date_range(idx[0], idx[-1], freq=frequency).difference(idx)) if frequency else None
    aligned = (idx == idx.floor(frequency)).all() if frequency else True
    numeric = frame.select_dtypes(include='number')
    import numpy as np
    valid = np.isfinite(numeric.to_numpy()).all()
    if {'open', 'high', 'low', 'close'} <= set(frame):
        valid = valid and bool((frame[['open', 'high', 'low', 'close']] > 0).all().all())
        valid = valid and bool(((frame.high >= frame[['open', 'close', 'low']].max(axis=1)) &
                               (frame.low <= frame[['open', 'close', 'high']].min(axis=1))).all())
    return dict(first_ts=str(idx[0]), last_ts=str(idx[-1]), observations=len(idx), gaps=gaps,
                status='VALID' if valid and aligned and (gaps in (None, 0)) else 'DATA_GAPS_OR_INVALID')


def load_stream(root, instrument, stream):
    directory = Path(root) / instrument / stream
    manifests = sorted(directory.glob('*-采集清单.json'))
    if not manifests:
        return pd.DataFrame(), dict(status='MISSING')
    latest = json.loads(manifests[-1].read_text(encoding='utf-8'))
    manifest = latest
    if not manifest.get('file'):
        for prior in reversed(manifests[:-1]):
            candidate = json.loads(prior.read_text(encoding='utf-8'))
            if candidate.get('file'):
                manifest = dict(candidate, latest_attempt_status=latest.get('status'),
                                latest_attempt_error=latest.get('error'))
                break
    filename = manifest.get('file')
    if not filename:
        return pd.DataFrame(), manifest
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError('manifest path escapes data directory')
    frame = pd.read_parquet(path)
    return frame, dict(manifest, data_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def coverage_audit(root, instruments):
    rows, data = [], {}
    for instrument in instruments:
        row = dict(instrument=instrument, list_time=None, trade_first_ts=None,
                   instrument_spec_first_ts=None, position_tier_first_ts=None,
                   spec_status='MISSING', execution_status='E0')
        data[instrument] = {}
        for stream, (_, _, frequency) in STREAMS.items():
            frame, manifest = load_stream(root, instrument, stream)
            quality = inspect_frame(frame, frequency=frequency)
            row.update({f'{stream}_{k}': v for k, v in quality.items()})
            row[f'{stream}_source'] = manifest.get('source')
            row[f'{stream}_sha256'] = manifest.get('data_sha256')
            row[f'{stream}_download_status'] = manifest.get('status')
            row[f'{stream}_error'] = manifest.get('error')
            data[instrument][stream] = frame
        # An API rate history has no independent evidence that every settlement
        # event is present. Never infer completeness from 8-hour spacing.
        rows.append(row)
    return rows, data
