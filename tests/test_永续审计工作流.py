from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from inv_trend.application.perpetual_audit.认证数据 import load_certified
from inv_trend.application.perpetual_audit.审计服务 import run_audit
from inv_trend.application.perpetual_audit.研究协议 import ExperimentRegistry
from tests.test_永续研究协议 import spec


def test_missing_data_produces_e0_not_fabricated_windows(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / '报告'
    report = run_audit(root, data_root=tmp_path / '空数据', output=output)
    assert report['evidence_level'] == 'E0'
    assert report['selected_trial_rank'] is None
    assert len(report['candidates']) == 12
    assert all(c['metrics'] is None for c in report['candidates'])
    assert pd.read_csv(output / '训练验证测试区间.csv').empty
    assert pd.read_csv(output / '永续逐笔账本.csv').empty
    assert len(ExperimentRegistry(output / '实验登记.jsonl').read()) == 24
    assert 'E0' in (output / '修改前后回测对比.html').read_text(encoding='utf-8')


def test_certified_input_requires_hash_and_no_spec_gaps(tmp_path):
    manifest = dict(schema_version='perpetual-input-v1', instruments={})
    index = pd.date_range('2019-01-01', periods=445, tz='UTC')
    t = index[-1]
    daily = pd.DataFrame(dict(timestamp=index, open=100., high=101., low=99., close=100., volume=10.))
    minute = pd.DataFrame(dict(timestamp=pd.date_range(t, periods=1440, freq='min'),
                               open=100., high=101., low=99., close=100., volume=10.))
    funding = pd.DataFrame(dict(timestamp=[t], funding_rate=[.001], mark_price_at_event=[100.]))
    for symbol in ('BTC-USDT-SWAP', 'ETH-USDT-SWAP'):
        refs = {}
        for kind, frame in (('daily', daily), ('minute', minute), ('marks', minute), ('funding', funding)):
            path = tmp_path / f'{symbol}-{kind}.parquet'
            frame.to_parquet(path, index=False)
            refs[kind] = dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), source='SYNTHETIC_TEST_ONLY')
        refs['funding'].update(coverage_start=str(t), coverage_end=str(t + pd.Timedelta(hours=23, minutes=59)),
                               settlement_schedule_evidence='SYNTHETIC_ONE_EVENT')
        path = tmp_path / f'{symbol}-规格.json'
        path.write_text(json.dumps([asdict(replace(spec(), instrument=symbol))]), encoding='utf-8')
        refs['specs'] = dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), source='SYNTHETIC_TEST_ONLY')
        manifest['instruments'][symbol] = refs
    path = tmp_path / '认证输入.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    bundle = load_certified(tmp_path)
    assert bundle.status == 'SIMULATED_SPEC'
    assert bundle.start == t
    manifest['instruments']['BTC-USDT-SWAP']['daily']['sha256'] = 'invalid'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        load_certified(tmp_path)
