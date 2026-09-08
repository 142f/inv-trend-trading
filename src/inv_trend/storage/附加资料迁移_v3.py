"""导入此前冻结的精确行情与B6摘要；绝不把旧摘要冒充新执行或完整账本。"""
from __future__ import annotations
import hashlib,json,zipfile
from pathlib import Path
from .结构化存储_v3 import UnifiedStore,digest,utcnow,StoreError
from .研究结果_v3 import import_market_csv,ResultRepository

FIXED_CSV='processed_data/backtest_ready/metal_tech_core/metal_tech_core_d1_backtest_ready.csv'
FIXED_HASH='7f232ee80f02e0df8d86699cbf7e26a0c71a6e7ebaf43f22547d7cf49b90f962'

def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def import_supplements(store,*,research_data=None,evidence=None):
    store=store if isinstance(store,UnifiedStore) else UnifiedStore(store)
    result={}
    if research_data:
        path=Path(research_data)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                hits=[n for n in z.namelist() if n.endswith(FIXED_CSV)]
                if len(hits)!=1:raise StoreError('压缩包中冻结行情路径缺失或不唯一')
                data=z.read(hits[0])
        else:data=path.read_bytes()
        import_market_csv(store,data,FIXED_CSV,FIXED_HASH)
        result['fixed_input_rows']=store.rows('SELECT row_count FROM market_datasets_v3 WHERE dataset_hash=?',(FIXED_HASH,))[0]['row_count']
        result['fixed_dataset_hash']=FIXED_HASH
    if evidence:
        path=Path(evidence);archive_hash=file_hash(path)
        prefix='发布研究_B6/';logical='backtests/历史B6'
        with zipfile.ZipFile(path) as z:
            docs=[n for n in z.namelist() if n.startswith(prefix) and n.endswith('.json')]
            identity=prefix+'实验协议冻结.json'
            if identity not in docs:raise StoreError('归档缺少B6冻结协议')
            # Foreign-key dependency: protocol precedes locks and runs.
            docs=sorted(docs,key=lambda n:(n!=identity,n.endswith('/运行记录.json'),n))
            repo=ResultRepository(store);runs=0
            for name in docs:
                key=logical+'/'+name[len(prefix):];data=z.read(name)
                if name.endswith('/运行记录.json'):
                    repo.publish(key.rsplit('/',1)[0],{},json.loads(data),summary_only=True,source_bytes=data);runs+=1
                else:store.put_document(key,data,kind='historical_research')
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO retained_sources VALUES(?,?,?,?,?)',
                (path.name,archive_hash,path.stat().st_size,'B6_SUMMARIES_ONLY_EXTERNAL_LEDGER',utcnow()))
            store.audit('IMPORT_HISTORICAL_SUMMARIES',archive_hash,{'documents':len(docs),'runs':runs,'ledger_imported':False,'external_archive':path.name},db=db)
        result.update(runs=runs,documents=len(docs),archive_sha256=archive_hash,details_imported=False)
    return result
