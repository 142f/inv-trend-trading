"""读取同库的运行、表哈希、协议和参数锁。历史摘要与本次完整执行严格区分。"""
from __future__ import annotations
import json
from .结构化存储_v3 import StoreError
from .研究结果_v3 import store_for,ResultRepository


def verify_database_research(directory):
    store=store_for(directory)
    if store is None:raise StoreError('不是v3数据库研究路径')
    prefix=store.key(directory)
    identity=store.read_document(prefix+'/实验协议冻结.json')
    rows=store.rows('SELECT * FROM result_runs WHERE logical_path LIKE ?',(prefix+'/%',))
    if not rows:raise StoreError('没有运行记录')
    repo=ResultRepository(store);tables=0;summary_only=0
    for run in rows:
        metadata=store.read_document(run['metadata_path'])
        if run['data_version']!=identity['protocol']['dataset_sha256']:raise StoreError('运行数据版本与冻结协议不一致')
        if run['code_hash']!=identity['source_sha256']:raise StoreError('运行源码版本与冻结协议不一致')
        current=store.rows('SELECT * FROM result_tables WHERE run_id=?',(run['run_id'],))
        if run['status']=='SUMMARY_ONLY':
            summary_only+=1
            if current:raise StoreError('摘要模式错误混入完整账本')
        else:
            expected=metadata.get('table_hashes',{})
            if len(expected)!=len(current) or not current:raise StoreError('完整运行缺少表或哈希')
            for t in current:
                if expected.get(t['table_name'])!=t['sha256']:raise StoreError('运行记录与结果表哈希不一致')
                repo.read_table(run['logical_path'],t['table_name']);tables+=1
    summary=store.read_document(prefix+'/完整迭代摘要.json')
    locks=0
    for step in summary:
        for lock in step['locks']:
            key=f"{prefix}/参数锁定/{step['iteration']}/{lock['window_id']}.json"
            if store.read_document(key)!=lock:raise StoreError('锁定参数与迭代摘要不一致')
            rows2=store.rows('SELECT * FROM wf_windows_v3 WHERE study_id=? AND iteration=? AND window_id=?',(prefix,step['iteration'],lock['window_id']))
            if len(rows2)!=1:raise StoreError('参数锁结构化索引缺失')
            locks+=1
    return {'verified':True,'storage_schema':3,'runs':len(rows),'tables':tables,'locks':locks,
            'summary_only_runs':summary_only,'complete_runs':len(rows)-summary_only,
            'source_sha256':identity['source_sha256'],'data_sha256':identity['protocol']['dataset_sha256'],
            'limitation':'SUMMARY_ONLY只验证历史摘要，外部详细账本未冒充已入库；一致性不等于盲测或实盘认证'}
