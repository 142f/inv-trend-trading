"""可复现存储对比：明确区分字节I/O、SQL查询和未安装的Parquet解码器。"""
from __future__ import annotations
import json
import statistics
import tempfile
import time
from pathlib import Path
from .结构化存储_v3 import UnifiedStore, canonical, digest


def timed(call,repeat=7):
    call()  # warm cache, explicitly not a cold-disk benchmark
    samples=[]
    for _ in range(repeat):
        start=time.perf_counter_ns();call();samples.append((time.perf_counter_ns()-start)/1e6)
    return {'median_ms':statistics.median(samples),'samples_ms':samples}


def benchmark(source,destination):
    source=Path(source);s=UnifiedStore(destination)
    paths=sorted((source/'quality_reports').glob('*.json'))+sorted((source/'ingestion_reports').glob('*.json'))
    fields=['symbol','timeframe','quality_status','backtest_suitable','quality_score','actual_bars','missing_count']
    def legacy_query():
        out=[]
        for p in paths:
            x=json.loads(p.read_bytes())
            if not x.get('backtest_suitable'):
                out.append((p.relative_to(source).as_posix(),*[x.get(k) for k in fields]))
        return sorted(out)
    def sql_query():
        return sorted(tuple(x.values()) for x in s.rows('SELECT document_path,symbol,timeframe,status,backtest_suitable,quality_score,actual_bars,missing_count FROM quality_assessments WHERE backtest_suitable=0'))
    assert legacy_query()==sql_query(), 'SQL投影与原始JSON不一致'
    all_parquet=sorted(source.rglob('*.parquet'))
    sample=all_parquet[len(all_parquet)//2]
    key=sample.relative_to(source).as_posix();physical=s.resolve(key)
    assert sample.read_bytes()==physical.read_bytes()
    one=paths[0];dkey=one.relative_to(source).as_posix()
    assert one.read_bytes()==s.document_bytes(dkey)
    # Transaction batch test uses independent throwaway roots, never pollutes delivery data.
    payloads=[canonical({'run_id':str(i),'status':'SUCCESS','metrics':{'return':i/100}}) for i in range(100)]
    with tempfile.TemporaryDirectory(prefix='存储对比_') as tmp:
        tmp=Path(tmp);counter=[0]
        v=UnifiedStore(tmp/'db',create=True)
        def sql_write():
            counter[0]+=1
            with v.connect() as db:
                for i,b in enumerate(payloads):v.put_document(f'metadata/批次{counter[0]}/{i}.json',b,db=db)
        from inv_trend.data.storage import DataLake
        old=DataLake(tmp/'files')
        def file_write():
            counter[0]+=1
            for i,b in enumerate(payloads):old.write_json(json.loads(b),Path('manifests')/f'{counter[0]}-{i}.json')
        writes={'old':timed(file_write,5),'new':timed(sql_write,5)}
    objects=s.rows('SELECT count(*) n, sum(stored_size) bytes FROM data_objects')[0]
    aliases=s.rows('SELECT count(*) n, sum(source_size) bytes FROM data_aliases')[0]
    s.checkpoint()
    dbfiles=[p for p in s.root.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','.lock'))]
    return {'query_quality':{'old':timed(legacy_query),'new':timed(sql_query),'equal':True},
        'document_read':{'old':timed(one.read_bytes),'new':timed(lambda:s.document_bytes(dkey)),'equal':True},
        'parquet_byte_read':{'old':timed(sample.read_bytes),'new':timed(physical.read_bytes),'equal':True,
             'note':'读取相同Parquet字节，不是列解码或真实回测性能'},
        'write_100_records':writes,'space_bytes':sum(p.stat().st_size for p in dbfiles),
        'file_count':len(dbfiles),'objects':objects,'aliases':aliases,
        'exact_duplicate_file_count':aliases['n']-objects['n'],
        'query_plan':s.rows('EXPLAIN QUERY PLAN SELECT document_path FROM quality_assessments WHERE backtest_suitable=0'),
        'cache_mode':'warm OS cache; sequential microbenchmark; median; no concurrent load'}
