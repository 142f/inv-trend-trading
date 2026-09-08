"""同一输入、算法和36组网格，对比在线特征复用开/关；不与不同策略作无效速度比较。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import statistics
import time
from inv_trend.core.阶段协议_v1 import digest
from inv_trend.core.在线策略_v1 import candidate_grid
from inv_trend.core.事件账户_v1 import ExecutionConfig
from inv_trend.data.时点行情_v1 import PointInTimeRepository
from inv_trend.storage.阶段运行_v1 import StageRepository
from inv_trend.application.多方法研究_v1 import train_grid, code_hash


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,default=Path('data'))
    p.add_argument('--output',type=Path,default=Path('性能核验_v1.json'))
    p.add_argument('--repetitions',type=int,default=3)
    args=p.parse_args()
    if args.repetitions < 1: p.error('repetitions must be positive')
    repository=StageRepository(args.data)
    datasets=repository.store.rows('SELECT dataset_hash FROM market_datasets_v3')
    if len(datasets) != 1: raise ValueError('benchmark requires one explicitly pinned dataset')
    version=datasets[0]['dataset_hash']
    with PointInTimeRepository(args.data,version,allowed_before='2020-01-01T00:00:00+00:00') as source:
        bars=source.history('QQQ',end='2020-01-01T00:00:00+00:00',limit=887)[:-5]
    times={True:[],False:[]}; hashes=[]
    for repeat in range(args.repetitions):
        # Alternate ordering to avoid always giving one implementation the warm-cache position.
        for cached in ((False,True) if repeat%2==0 else (True,False)):
            started=time.perf_counter()
            result=train_grid(bars,candidate_grid(),ExecutionConfig(),cache_features=cached)
            times[cached].append(time.perf_counter()-started)
            hashes.append(digest(result))
    if len(set(hashes)) != 1: raise AssertionError('optimization changed exact trial output')
    grid=candidate_grid(); keys={(c.lookback,c.exit_lookback) for c in grid}
    receipt={'code_version':code_hash(),'dataset_version':version,'scope':'identical bounded historical TRAIN replay',
        'repetitions':args.repetitions,'training_bars':len(bars),'parameters':len(grid),'feature_configurations':len(keys),
        'cached_seconds':times[True],'uncached_seconds':times[False],
        'cached_median':statistics.median(times[True]),'uncached_median':statistics.median(times[False]),
        'speedup':statistics.median(times[False])/statistics.median(times[True]),'exact_output_hash':hashes[0],
        'outputs_identical':True,'source_reads_per_research_window':1,'holdout_accessed':False,
        'limitation':'single-host microbenchmark; no claim about old Turtle engine or production latency'}
    raw=json.dumps(receipt,ensure_ascii=False,indent=2)
    repository.store.put_document(f'reports/阶段性能/{digest(receipt)}.json',raw.encode(),kind='benchmark')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:f.write(raw)
    print(raw)

if __name__=='__main__':main()
