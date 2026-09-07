"""统一研究表、参数锁、回测摘要、逐笔账本。逻辑CSV仅为兼容内存视图。"""
from __future__ import annotations
import io
import json
from pathlib import Path
import zlib
import pandas as pd
import math
from .结构化存储_v3 import UnifiedStore, CatalogPath, DB_RELATIVE, canonical, digest, utcnow, StoreError
from .阶段制品 import encode_frame, decode_frame


def store_for(path):
    p=Path(str(path)).absolute()
    for root in (p,*p.parents):
        if (root/DB_RELATIVE).is_file():return UnifiedStore(root)
    return None


def research_path(path):
    return CatalogPath(Path(str(path)).absolute()) if store_for(path) else Path(path)


class ResultRepository:
    def __init__(self,store):
        self.store=store if isinstance(store,UnifiedStore) else UnifiedStore(store)

    def publish(self,folder,frames,metadata,*,summary_only=False,source_bytes=None):
        logical=self.store.key(folder)
        encoded={};hashes={}
        for name,frame in frames.items():
            series=isinstance(frame,pd.Series)
            value=encode_frame(frame.to_frame() if series else frame)
            value['was_series']=series
            value['series_name']=frame.name if series else None
            base=frame.to_frame() if series else frame
            value['column_index']={'name':base.columns.name,'dtype':str(base.columns.dtype),
                'range':[base.columns.start,base.columns.stop,base.columns.step] if isinstance(base.columns,pd.RangeIndex) else None}
            value['row_range']=[base.index.start,base.index.stop,base.index.step] if isinstance(base.index,pd.RangeIndex) else None
            nulls=[]
            for ci,(column,dtype) in enumerate(base.dtypes.items()):
                if str(dtype)!='object':continue
                for ri,item in enumerate(base[column]):
                    typ='NA' if item is pd.NA else 'NaT' if item is pd.NaT else 'NaN' if isinstance(item,float) and math.isnan(item) else None
                    if typ:nulls.append([ri,ci,typ])
            value['object_nulls']=nulls
            b=canonical(value);hashes[name]=digest(b);encoded[name]=value
        identity=digest(canonical({'logical':logical,'metadata':metadata,'tables':hashes}))
        run_id=digest(canonical(['run',logical]))
        parameter_hash=digest(canonical(metadata.get('parameters',{})))
        meta_key=f'{logical}/运行记录.json'
        record={**metadata,'database_run_id':run_id,'table_hashes':hashes,'storage_schema_version':3}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT result_hash FROM result_runs WHERE run_id=?',(run_id,)).fetchone()
            if old:
                if old[0]!=identity:raise StoreError('同一运行ID不能覆盖不同结果')
                return self.store.read_document(meta_key)
            if source_bytes is not None and json.loads(source_bytes)!=metadata:
                raise StoreError('原始回放元数据与导入内容不一致')
            self.store.put_document(meta_key,source_bytes if source_bytes is not None else record,kind='backtest_run',db=db)
            study=db.execute('SELECT * FROM studies_v3 WHERE ? LIKE study_id || "/%" ORDER BY length(study_id) DESC LIMIT 1',(logical,)).fetchone()
            db.execute('INSERT OR IGNORE INTO result_parameters VALUES(?,?,?)',(parameter_hash,canonical(metadata.get('parameters',{})).decode(),utcnow()))
            db.execute('INSERT INTO result_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (run_id,logical,'backtests','SUMMARY_ONLY' if summary_only else 'SUCCEEDED',metadata.get('strategy_version',study['strategy_version'] if study else None),
                 metadata.get('source_sha256',study['code_hash'] if study else None),parameter_hash,metadata.get('data_version',study['data_version'] if study else None),metadata.get('scope','OOS' if 'iteration' in metadata else 'STRESS'),
                 metadata.get('train_start'),metadata.get('train_end'),metadata.get('test_start'),metadata.get('test_end'),
                 metadata.get('started_at'),metadata.get('created_at',utcnow()),meta_key,identity,None))
            if study:
                db.execute('INSERT INTO study_runs_v3 VALUES(?,?,?)',(study['study_id'],run_id,'HISTORICAL_SUMMARY' if summary_only else 'EXECUTED'))
            for name,value in encoded.items():
                self._write_table(db,run_id,name,value,hashes[name])
            self._metrics(db,run_id,metadata.get('metrics',{}))
            for fold in metadata.get('folds',[]):
                self._metrics(db,run_id,fold.get('metrics',{}),fold.get('window_id',''))
            self.store.audit('PUBLISH_RESULT',run_id,{'identity':identity,'scope':metadata.get('scope'),'tables':hashes,'summary_only':summary_only},db=db)
        return record

    def _metrics(self,db,run_id,metrics,window=''):
        for name,value in metrics.items():
            numeric=float(value) if isinstance(value,(int,float,bool)) and value is not None else None
            text=None if isinstance(value,(int,float,bool)) or value is None else str(value)
            db.execute('INSERT INTO result_metrics VALUES(?,?,?,?,?,?)',(run_id,window,'',name,numeric,text))

    def _write_table(self,db,run_id,name,value,hash_value):
        body={k:v for k,v in value.items() if k not in ('values','index')}
        db.execute('INSERT INTO result_tables VALUES(?,?,?,?,?)',(run_id,name,len(value['values']),canonical(body).decode(),hash_value))
        rows=[];cols=value['columns']
        def number(v):
            return float(v) if isinstance(v,(int,float)) and not isinstance(v,bool) else None
        for i,(idx,vals) in enumerate(zip(value['index'],value['values'])):
            rec=dict(zip(cols,vals));raw=canonical([idx,vals])
            compressed=len(raw)>256;payload=zlib.compress(raw,3) if compressed else raw
            tm=rec.get('time',rec.get('exit_time',rec.get('timestamp',idx if value['index_kind']=='datetime' else None)))
            rows.append((run_id,name,i,str(tm) if tm is not None else None,rec.get('symbol',rec.get('instrument')),
                         str(rec.get('side')) if rec.get('side') is not None else None,
                         number(rec.get('pnl',rec.get('net_pnl'))),number(rec.get('price',rec.get('fill_price'))),
                         number(rec.get('quantity',rec.get('qty'))),payload,'zlib' if compressed else 'identity'))
        db.executemany('INSERT INTO result_rows VALUES('+','.join('?'*11)+')',rows)

    def read_table(self,folder,name):
        logical=self.store.key(folder)
        with self.store.connect(readonly=True) as db:
            row=db.execute('SELECT t.* FROM result_tables t JOIN result_runs r USING(run_id) WHERE r.logical_path=? AND t.table_name=?',(logical,name)).fetchone()
            if not row:raise FileNotFoundError(f'{logical}/{name}')
            content=dict(json.loads(row['schema_json']));records=[]
            for item in db.execute('SELECT payload,codec FROM result_rows WHERE run_id=? AND table_name=? ORDER BY ordinal',(row['run_id'],name)):
                raw=zlib.decompress(item['payload']) if item['codec']=='zlib' else bytes(item['payload'])
                records.append(json.loads(raw))
        content['index']=[r[0] for r in records];content['values']=[r[1] for r in records]
        if len(records)!=row['row_count'] or digest(canonical(content))!=row['sha256']:
            raise StoreError('结果表行数或哈希不一致')
        frame=decode_frame(content)
        if content.get('column_index'):
            c=content['column_index']
            frame.columns=pd.RangeIndex(*c['range'],name=c['name']) if c['range'] is not None else pd.Index(content['columns'],dtype=c['dtype'],name=c['name'])
        if content.get('row_range') is not None:
            frame.index=pd.RangeIndex(*content['row_range'],name=content.get('index_name'))
        for ri,ci,typ in content.get('object_nulls',[]):
            frame.iat[ri,ci]={'NA':pd.NA,'NaT':pd.NaT,'NaN':float('nan')}[typ]
        if content.get('was_series'):
            result=frame.iloc[:,0]
            if 'series_name' in content:result.name=content['series_name']
            return result
        return frame

    def has_table(self,folder,name):
        return bool(self.store.rows('SELECT 1 FROM result_tables t JOIN result_runs r USING(run_id) WHERE r.logical_path=? AND t.table_name=?',(self.store.key(folder),name)))

    def validate(self,prefix=None):
        rows=self.store.rows('SELECT r.logical_path,t.table_name FROM result_runs r JOIN result_tables t USING(run_id) WHERE r.logical_path LIKE ?',((self.store.key(prefix)+'/%') if prefix else '%',))
        for row in rows:self.read_table(row['logical_path'],row['table_name'])
        return {'ok':True,'tables':len(rows)}


def read_research_csv(path,**kwargs):
    store=store_for(path)
    if store:
        p=Path(str(path));name=p.name.removesuffix('.csv.gz').removesuffix('.csv')
        repo=ResultRepository(store)
        if repo.has_table(p.parent,name):
            f=repo.read_table(p.parent,name)
            # Match v2 CSV presentation semantics without writing CSV to disk.
            text=f.to_csv(index=isinstance(f,pd.Series))
            try:return pd.read_csv(io.StringIO(text),**kwargs)
            except pd.errors.EmptyDataError:return pd.DataFrame()
    return pd.read_csv(path,**kwargs)


def import_market_csv(store,data,source_name,expected_hash):
    if digest(data)!=expected_hash:raise StoreError('固定回测行情来源SHA256不符')
    frame=pd.read_csv(io.BytesIO(data),float_precision='round_trip')
    expected=['date','symbol','open','high','low','close','volume','spread','source','timeframe']
    if list(frame.columns)!=expected:raise StoreError('固定回测行情Schema发生变化')
    from inv_trend.core.阶段契约 import frame_fingerprint
    frame_hash=frame_fingerprint(frame)
    key=f'metadata/固定输入/{expected_hash}.csv'
    with store.connect() as db:
        if db.execute('SELECT 1 FROM market_datasets_v3 WHERE dataset_hash=?',(expected_hash,)).fetchone():return
        store.put_document(key,data,kind='market_source',db=db)
        schema=canonical({str(k):str(v) for k,v in frame.dtypes.items()}).decode()
        db.execute('INSERT INTO market_datasets_v3(dataset_hash,source_document,row_count,schema_json,frame_hash,status,created_at) VALUES(?,?,?,?,?,?,?)',(expected_hash,key,len(frame),schema,frame_hash,'RESEARCH_ONLY',utcnow()))
        dataset_id=db.execute('SELECT dataset_id FROM market_datasets_v3 WHERE dataset_hash=?',(expected_hash,)).fetchone()[0]
        values=[]
        for i,row in enumerate(frame.itertuples(index=False,name=None)):
            values.append((dataset_id,i,*row))
        db.executemany('INSERT INTO market_bars_v3 VALUES('+','.join('?'*12)+')',values)
        db.execute('INSERT OR IGNORE INTO retained_sources VALUES(?,?,?,?,?)',(source_name,expected_hash,len(data),'FIXED_RESEARCH_INPUT',utcnow()))
        store.audit('IMPORT_FIXED_INPUT',expected_hash,{'source':source_name,'rows':len(frame),'policy':'保留此前冻结输入，不替换为新行情'},db=db)


def load_market_frame(store,dataset_hash):
    rows=store.rows('SELECT * FROM market_datasets_v3 WHERE dataset_hash=?',(dataset_hash,))
    if not rows:raise FileNotFoundError(dataset_hash)
    spec=rows[0]
    # Audit source bytes and typed rows independently.
    if digest(store.document_bytes(spec['source_document']))!=dataset_hash:raise StoreError('固定行情原文已变化')
    data=store.rows('SELECT date,symbol,open,high,low,close,volume,spread,source,timeframe FROM market_bars_v3 WHERE dataset_id=? ORDER BY ordinal',(spec['dataset_id'],))
    frame=pd.DataFrame(data,columns=['date','symbol','open','high','low','close','volume','spread','source','timeframe'])
    for c,dtype in json.loads(spec['schema_json']).items():frame[c]=frame[c].astype(dtype)
    from inv_trend.core.阶段契约 import frame_fingerprint
    if len(frame)!=spec['row_count'] or frame_fingerprint(frame)!=spec['frame_hash']:raise StoreError('结构化行情与原始DataFrame不一致')
    return frame
