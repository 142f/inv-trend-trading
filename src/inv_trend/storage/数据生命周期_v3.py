"""默认只读清理计划。任何来源、父版本、运行/审计引用都保护正式数据。"""
from __future__ import annotations
from datetime import datetime,timezone,timedelta
from pathlib import Path
import os
import sqlite3
from .结构化存储_v3 import UnifiedStore,StoreError,utcnow,digest
from inv_trend.data.locking import FileLock


def cleanup(store,*,apply=False,min_age_days=7):
    store=store if isinstance(store,UnifiedStore) else UnifiedStore(store)
    # Nothing in source_inventory is deleted. Only unregistered leftovers from failed writes qualify.
    if min_age_days<1:raise ValueError('隔离期限至少一天')
    cutoff=datetime.now(timezone.utc)-timedelta(days=min_age_days)
    result=[]
    with FileLock(store.root/'metadata'/'提交与回收.lock',timeout=30):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            registered={r[0] for r in db.execute('SELECT path FROM data_objects')}
            for stage in ('raw','processed','features','signals','backtests','reports'):
                for p in (store.root/stage).rglob('*'):
                    if not p.is_file():continue
                    key=p.relative_to(store.root).as_posix()
                    if p.is_symlink():raise StoreError('清理不接受符号链接')
                    if key in registered:continue
                    age=datetime.fromtimestamp(p.stat().st_mtime,timezone.utc)
                    # Published report files not registered yet are not disposable by name alone.
                    if not p.name.startswith(('暂存_','.tmp-','数据_')) or age>cutoff:continue
                    # Hash identity protects a canonical object if an import was interrupted.
                    h=digest(p.read_bytes())
                    if db.execute('SELECT 1 FROM data_objects WHERE stored_sha256=?',(h,)).fetchone():continue
                    item={'path':key,'sha256':h,'bytes':p.stat().st_size,'action':'DELETE_UNREFERENCED_TEMP','applied':apply}
                    result.append(item)
                    if apply:
                        store.audit('DELETE_UNREFERENCED_TEMP',key,item,db=db)
                        p.unlink()
    return result


def backup_database(store,destination):
    store=store if isinstance(store,UnifiedStore) else UnifiedStore(store)
    destination=Path(destination)
    if destination.exists():raise FileExistsError(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with FileLock(store.root/'metadata'/'提交与回收.lock',timeout=30):
        with store.connect(readonly=True) as src:
            with sqlite3.connect(destination) as dst:src.backup(dst)
    return destination


def ready_to_replace(store,*,strict_parquet=False):
    import importlib.util
    store=store if isinstance(store,UnifiedStore) else UnifiedStore(store)
    result=store.validate()
    from .研究结果_v3 import load_market_frame,ResultRepository
    for r in store.rows('SELECT dataset_hash FROM market_datasets_v3'):load_market_frame(store,r['dataset_hash'])
    ResultRepository(store).validate()
    parquet_available=importlib.util.find_spec('pyarrow') is not None
    result['pyarrow_available']=parquet_available
    result['parquet_decode_verified']=False
    if strict_parquet:
        if not parquet_available:raise StoreError('严格Parquet解码验收需要安装PyArrow；字节一致不冒充解码验证')
        import pyarrow.parquet as pq
        for r in store.rows("SELECT * FROM data_objects WHERE format='parquet'"):
            table=pq.read_table(store.physical(r['path']))
            if r['rows_count'] is not None and table.num_rows!=r['rows_count']:
                raise StoreError('真实Parquet解码行数不一致')
        result['parquet_decode_verified']=True
    result['original_lineage_warnings']=store.rows('SELECT category,count(*) count FROM migration_issues GROUP BY category')
    return result
