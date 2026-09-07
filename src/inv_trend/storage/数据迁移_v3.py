"""可重入、离线、来源只读的数据目录迁移。保存全部唯一数据及原始审计字节。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import time
from .结构化存储_v3 import UnifiedStore, StoreError, normalize_ref, digest, utcnow


def parquet_footer(data):
    """仅读取标准 Thrift footer；不解码或修改数据页，不替代 PyArrow 行情正确性测试。"""
    if data[:4]!=b'PAR1' or data[-4:]!=b'PAR1':
        raise StoreError('无效 Parquet 魔数')
    size=struct.unpack('<I',data[-8:-4])[0]
    if size<=0 or size>len(data)-12:
        raise StoreError('无效 Parquet footer 长度')
    try:
        from thrift.transport.TTransport import TMemoryBuffer
        from thrift.protocol.TCompactProtocol import TCompactProtocol
        from thrift.Thrift import TType
    except ImportError:
        return {'rows_count':None,'schema':{'inspection':'magic_only','reason':'thrift not installed'}}
    proto=TCompactProtocol(TMemoryBuffer(data[-8-size:-8]))
    def value(t,depth=0):
        if depth>30:raise StoreError('Parquet footer 嵌套异常')
        methods={TType.BOOL:'readBool',TType.BYTE:'readByte',TType.I16:'readI16',TType.I32:'readI32',TType.I64:'readI64',TType.DOUBLE:'readDouble',TType.STRING:'readBinary'}
        if t in methods:return getattr(proto,methods[t])()
        if t==TType.STRUCT:
            out={};proto.readStructBegin()
            while True:
                _,typ,key=proto.readFieldBegin()
                if typ==TType.STOP:break
                out[key]=value(typ,depth+1);proto.readFieldEnd()
            proto.readStructEnd();return out
        if t in (TType.LIST,TType.SET):
            typ,n=proto.readListBegin() if t==TType.LIST else proto.readSetBegin()
            if n>1000000:raise StoreError('Parquet footer 列表过大')
            out=[value(typ,depth+1) for _ in range(n)]
            proto.readListEnd() if t==TType.LIST else proto.readSetEnd()
            return out
        if t==TType.MAP:
            a,b,n=proto.readMapBegin();out={value(a,depth+1):value(b,depth+1) for _ in range(n)};proto.readMapEnd();return out
        raise StoreError(f'不支持的 Thrift 类型 {t}')
    footer=value(TType.STRUCT)
    schema=[{'name':x.get(4,b'').decode('utf-8'),'type':x.get(1),'repetition':x.get(3),'logical':str(x.get(10,''))} for x in footer.get(2,[])]
    return {'rows_count':footer.get(3),'schema':schema}


def _stage(key):
    first=key.split('/')[0]
    if first=='raw':return 'raw'
    if first in ('curated','normalized','legacy','quarantine','reviews'):return 'processed'
    if first=='features':return 'features'
    if first in ('signals','backtests','reports'):return first
    return 'metadata'


def migrate(source, destination, *, round_id=1, compress=True, indexes=True):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if (source/'metadata/研究目录_v3.sqlite3').exists():
        raise StoreError('来源已是v3；请使用备份/恢复，不能按旧目录再次迁移')
    if not source.is_dir():raise FileNotFoundError(source)
    if source==destination or source in destination.parents or destination in source.parents:
        raise StoreError('迁移目标必须与来源独立，禁止原地覆盖')
    files=sorted(x for x in source.rglob('*') if x.is_file())
    if any(p.is_symlink() for p in source.rglob('*')):
        raise StoreError('来源含符号链接，需先人工核对')
    fingerprint=digest(json.dumps([(p.relative_to(source).as_posix(),digest(p.read_bytes())) for p in files],ensure_ascii=False).encode())
    if destination.exists():
        existing=UnifiedStore(destination)
        row=existing.rows("SELECT value FROM store_meta WHERE key='source_fingerprint'")
        status=existing.rows("SELECT value FROM store_meta WHERE key='migration_status'")
        if row and row[0]['value']==fingerprint and status and status[0]['value']=='COMPLETE':
            check=existing.validate(source=source)
            if not check['ok']:raise StoreError('已迁移目录核验失败')
            return {'reused':True,**check}
        raise FileExistsError('目标已存在且来源版本不一致，拒绝覆盖')
    started=time.perf_counter()
    store=UnifiedStore(destination,create=True,compress=compress,indexes=indexes)
    try:
        with store.connect() as db:
            db.execute("INSERT INTO store_meta VALUES('migration_status','STAGING')")
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for p in files:
                key=p.relative_to(source).as_posix();data=p.read_bytes();sha=digest(data)
                is_document=(p.suffix in ('.json','.jsonl','.md','.sqlite3') and not key.startswith('raw/'))
                if is_document:
                    store.put_document(key,data,kind=key.split('/')[0],db=db)
                    target_kind='document';target_id=sha;action='IN_DATABASE'
                else:
                    info=parquet_footer(data) if p.suffix=='.parquet' else None
                    store.put_object(key,data,stage=_stage(key),info=info,db=db)
                    target_kind='object';target_id=sha;action='CONTENT_ADDRESS'
                db.execute('INSERT INTO source_inventory VALUES(?,?,?,?,?,?,1,?)',
                    (key,sha,len(data),action,target_kind,target_id,'保留原始字节；物理副本按SHA256合并'))
            old=source/'catalog.sqlite3'
            if old.exists():
                origin=sqlite3.connect(old.as_uri()+'?mode=ro',uri=True);origin.row_factory=sqlite3.Row
                try:
                    for name in ('datasets','current_versions','rollbacks'):
                        cols=[r[1] for r in origin.execute(f'PRAGMA table_info({name})')]
                        rows=origin.execute(f'SELECT * FROM {name}').fetchall()
                        if rows:
                            sql=f'INSERT INTO {name}('+','.join(cols)+') VALUES('+','.join('?'*len(cols))+')'
                            db.executemany(sql,[tuple(x) for x in rows])
                finally:origin.close()
            db.execute('INSERT INTO store_meta VALUES(?,?)',('source_fingerprint',fingerprint))
            db.execute('INSERT INTO store_meta VALUES(?,?)',('migration_round',str(round_id)))
            store.audit('MIGRATION_IMPORT',fingerprint,{'files':len(files),'bytes':sum(x.stat().st_size for x in files)},db=db)
        _build_versions(store)
        _build_links(store)
        if indexes:store.add_indexes()
        check=store.validate(source=source)
        if not check['ok']:raise StoreError(str(check['issues'][:5]))
        with store.connect() as db:
            db.execute("UPDATE store_meta SET value='COMPLETE' WHERE key='migration_status'")
            store.audit('MIGRATION_COMPLETE',fingerprint,{'source_files':len(files),'verified':True},db=db)
        store.checkpoint(vacuum=True)
        return {'reused':False,'seconds':time.perf_counter()-started,'source_files':len(files),
                'source_bytes':sum(x.stat().st_size for x in files),'target_files':sum(p.is_file() for p in destination.rglob('*')),
                'target_bytes':sum(p.stat().st_size for p in destination.rglob('*') if p.is_file()),
                'source_fingerprint':fingerprint,**check}
    except BaseException:
        # Do not delete evidence after a failed attempt; it cannot pass validation/cutover.
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO store_meta VALUES('migration_status','FAILED')")
        raise


def _build_versions(store):
    catalog=store.rows('SELECT * FROM datasets')
    instrument_map={x['instrument_id']:x for x in catalog}
    docs={x['logical_path'] for x in store.rows('SELECT logical_path FROM documents')}
    records=store.rows("SELECT a.*,o.rows_count FROM data_aliases a JOIN data_objects o ON a.object_id=o.object_id WHERE a.logical_path LIKE 'curated/%/bars.parquet' OR a.logical_path LIKE 'legacy/%/bars.parquet'")
    with store.connect() as db:
        for r in records:
            parts=dict(x.split('=',1) for x in r['logical_path'].split('/') if '=' in x)
            inst=parts['instrument'];version=parts['version'];tf=parts['timeframe']
            ref=instrument_map.get(inst,{})
            symbol=ref.get('symbol') or inst.split('.')[0]
            channel='legacy' if parts.get('asset_class')=='legacy' or r['logical_path'].startswith('legacy/') else 'curated'
            manifest_key=f'dataset_manifests/{version}.json';quality_key=f'quality_reports/{version}.json'
            manifest=store.read_document(manifest_key) if manifest_key in docs else {}
            quality_key=manifest.get('quality_report_path',quality_key)
            quality=store.read_document(quality_key) if quality_key in docs else {}
            db.execute('INSERT OR IGNORE INTO instruments_v3 VALUES(?,?,?,?,?,?)',
                (inst,symbol,parts.get('asset_class','unknown'),'UTC',ref.get('source'),'SOURCE_UNVERIFIED'))
            status=quality.get('quality_status') or ('LEGACY_ONLY' if channel=='legacy' else 'UNKNOWN')
            db.execute('INSERT INTO dataset_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (version,inst,symbol,tf,channel,r['logical_path'],None,manifest.get('publication_run_id',ref.get('run_id')),
                 manifest_key if manifest_key in docs else None,quality_key if quality_key in docs else None,
                 r['rows_count'],manifest.get('full_actual_start'),manifest.get('full_actual_end'),status,
                 manifest.get('published_at',ref.get('created_at',utcnow())),manifest.get('cleaning_rule_version')))
        for r in records:
            key=f"dataset_manifests/{r['dataset_version']}.json"
            if key in docs:
                parent=store.read_document(key).get('parent_dataset_version')
                if parent:
                    if db.execute('SELECT 1 FROM dataset_versions WHERE version=?',(parent,)).fetchone():
                        db.execute('UPDATE dataset_versions SET parent_version=? WHERE version=?',(parent,r['dataset_version']))
                    else:
                        db.execute('INSERT INTO migration_issues(severity,category,source_path,details) VALUES(?,?,?,?)',('WARNING','MISSING_PARENT',key,parent))
        for item in store.rows("SELECT logical_path FROM documents WHERE logical_path LIKE 'curated/symbol=%/current.json' OR logical_path LIKE 'legacy/symbol=%/current.json'"):
            key=item['logical_path'];p=store.read_document(key)
            parts=dict(x.split('=',1) for x in key.split('/') if '=' in x)
            rec=db.execute('SELECT * FROM dataset_versions WHERE version=?',(p.get('version'),)).fetchone()
            if not rec:
                db.execute('INSERT INTO migration_issues(severity,category,source_path,details) VALUES(?,?,?,?)',('ERROR','MISSING_CURRENT',key,str(p)))
                continue
            symbol=parts['symbol'];tf=parts['timeframe'];channel=rec['channel']
            db.execute('INSERT INTO dataset_heads VALUES(?,?,?,?,?,?,?)',(symbol,tf,channel,rec['version'],p.get('run_id','unknown'),1,utcnow()))
            old=db.execute('SELECT version FROM current_versions WHERE symbol=? AND timeframe=?',(symbol,tf)).fetchone()
            if old and old[0]!=rec['version']:
                raise StoreError('原始pointer与Catalog不一致，拒绝自动选择')
        store.audit('VERSION_INDEX','dataset_versions',{'count':len(records)},db=db)


def _build_links(store):
    aliases={r['logical_path']:r for r in store.rows('SELECT logical_path,object_id FROM data_aliases')}
    docs={r['logical_path']:r for r in store.rows('SELECT logical_path,sha256,kind FROM documents')}
    with store.connect() as db:
        for key,r in docs.items():
            if r['kind'] not in ('manifests','dataset_manifests'):continue
            payload=store.read_document(key);refs={}
            for path in payload.get('file_paths',[]):refs[path]=payload.get('file_hashes',{}).get(path)
            refs.update(payload.get('file_hashes',{}))
            for name,hashkey in [('curated_path','curated_sha256'),('quality_report_path','quality_report_sha256')]:
                if payload.get(name):refs[payload[name]]=payload.get(hashkey)
            for i,(raw,expected) in enumerate(refs.items()):
                try:target=normalize_ref(raw)
                except StoreError:target=str(raw).replace('\\','/')
                a=aliases.get(target);d=docs.get(target)
                actual=a['object_id'] if a else d['sha256'] if d else None
                status='VERIFIED' if actual and (not expected or expected==actual) else 'HASH_MISMATCH' if actual else 'SOURCE_REFERENCE_MISSING'
                db.execute('INSERT INTO lineage_links VALUES(?,?,?,?,?,?,?,?)',
                    (key,i,raw,expected,actual,a['object_id'] if a else None,target if d else None,status))
                if status!='VERIFIED':
                    db.execute('INSERT INTO migration_issues(severity,category,source_path,details) VALUES(?,?,?,?)',
                        ('WARNING',status,key,str(raw)))
        store.audit('LINEAGE_INDEX','lineage_links',{'policy':'保留既有缺失，不伪造哈希或审批'},db=db)
