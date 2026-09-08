"""阶段结果的确定性序列化与统一仓库发布。"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from .基础 import digest, encoded
from .仓库 import Storage


def _scalar(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def encode_frame(frame):
    return {
        'columns': list(frame.columns),
        'dtypes': [str(dtype) for dtype in frame.dtypes],
        'index': [_scalar(value) for value in frame.index],
        'index_kind': 'datetime' if isinstance(frame.index, pd.DatetimeIndex) else 'index',
        'index_dtype': str(frame.index.dtype),
        'index_name': frame.index.name,
        'index_freq': frame.index.freqstr if isinstance(frame.index, pd.DatetimeIndex) else None,
        'values': [[_scalar(value) for value in row] for row in frame.itertuples(index=False, name=None)],
    }


def decode_frame(value):
    frame = pd.DataFrame(value['values'], columns=value['columns'])
    for column, dtype in zip(value['columns'], value['dtypes']):
        if dtype.startswith('datetime64'):
            frame[column] = pd.to_datetime(frame[column], utc=',' in dtype).astype(dtype)
        else:
            frame[column] = frame[column].astype(dtype)
    if value['index_kind'] == 'datetime':
        frame.index = pd.DatetimeIndex(value['index'], dtype=value['index_dtype'],
                                      name=value['index_name'], freq=value.get('index_freq'))
    else:
        frame.index = pd.Index(value['index'], dtype=value['index_dtype'], name=value['index_name'])
    return frame


class StageStore:
    def __init__(self, root='.'):
        self.storage = Storage(root)

    def publish(self, stage, body, *, parameters=None):
        parameters = parameters or {}
        identity = digest({'stage': stage, 'body': body, 'parameters': parameters})
        self.storage.publish(stage, identity, 'stage', {'阶段结果.json': encoded(body)}, config=parameters)
        return identity

    def load(self, identity):
        manifest = self.storage.manifest(identity)
        item = next(item for item in manifest['files'] if item['logical_name'] == '阶段结果.json')
        self.storage._verify_manifest(manifest)
        return manifest, json.loads(self.storage._path(item['path']).read_bytes())
