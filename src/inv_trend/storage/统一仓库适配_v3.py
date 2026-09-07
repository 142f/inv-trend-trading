"""原 Storage/StageStore/Registry 接口的单库实现。新数据根不再生成 outputs/系统 的第二权威。"""
from __future__ import annotations
import json
from pathlib import Path
from .仓库 import Storage, StorageIntegrityError
from .结构化存储_v3 import UnifiedStore, CatalogPath, digest as bytes_digest, utcnow
from .基础 import digest, encoded, redact, segment


class DatabaseStorage(Storage):
    def __init__(self,root='.'):
        self.root=Path(root).resolve()
        self.catalog=UnifiedStore(self.root/'data')
        self.system=self.catalog.root/'metadata'
        self.path=self.catalog.path
        self.objects=self.catalog.root/'backtests'/'对象'
        self.lock_path=self.system/'提交与回收.lock'
        # Existing shared registry schema is installed in the SAME SQLite transaction authority.
        schema=Path(__file__).with_name('结构.sql').read_text(encoding='utf-8')
        with self.catalog.connect() as db:
            db.executescript(schema)
            checksum=bytes_digest(schema.encode())
            old=db.execute('SELECT checksum FROM schema_migrations WHERE version=1').fetchone()
            if old and old[0]!=checksum:raise StorageIntegrityError('共享仓库Schema版本不匹配')
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?,?)',(utcnow(),checksum))

    def _path(self,relative):
        raw=str(relative).replace('\\','/')
        if raw.startswith('data/'):return CatalogPath(self.catalog.root/raw[5:])
        if Path(raw).is_absolute():
            path=Path(raw)
            if self.catalog.root in path.parents:return CatalogPath(path)
        return super()._path(relative)

    def publish(self,task,run_id,report_date,files,*,facts=None,config=None,
                retention_class='RESULT',protection_reason='正式运行',fault=None):
        task,run_id,report_date=map(segment,(task,run_id,report_date))
        facts,config=redact(facts or {}),redact(config or {})
        identity=digest({'task':task,'date':report_date,'files':{k:bytes_digest(v) for k,v in files.items()},'facts':facts,'config':config})
        with self.lock():
            old=self.rows('SELECT * FROM runs WHERE run_id=?',(run_id,))
            if old:
                manifest=self.manifest(run_id)
                if manifest['identity']!=identity:raise StorageIntegrityError('不可变运行身份冲突')
                self._verify_manifest(manifest)
                return {i['logical_name']:self._path(i['path']) for i in manifest['files']}
            if fault:fault('STAGING')
            entries=[]
            with self.catalog.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for name,content in sorted(files.items()):
                    # normalize_ref rejects traversal before any persistent publication.
                    key=self.catalog.key(f'backtests/运行/{run_id}/{name}')
                    suffix=Path(name).suffix
                    if suffix in ('.json','.gz','.csv','.txt','.md'):
                        self.catalog.put_document(key,content,kind='stage_artifact',db=db)
                        info={'format':suffix.lstrip('.'),'schema_version':None,'schema_hash':None}
                    else:
                        info=self._validate(content,suffix)
                        stage='reports' if suffix=='.html' else 'backtests'
                        self.catalog.put_object(key,content,stage=stage,db=db)
                    h=bytes_digest(content)
                    entries.append({'logical_name':name,'hash':h,'size':len(content),'path':'data/'+key,**info})
                manifest={'schema_version':3,'task':task,'run_id':run_id,'report_date':report_date,'identity':identity,
                          'files':entries,'facts':facts,'config':config,'retention_class':retention_class,
                          'protection_reason':protection_reason}
                mkey=f'metadata/运行清单/{run_id}.json'
                self.catalog.put_document(mkey,encoded(manifest),kind='run_manifest',db=db)
                if fault:fault('FILES_PUBLISHED')
                seq=db.execute('SELECT COALESCE(MAX(commit_seq),0)+1 FROM runs').fetchone()[0]
                db.execute("INSERT INTO runs(run_id,task,report_date,status,phase,manifest_hash,manifest_path,config_hash,created_at,committed_at,commit_seq) VALUES(?,?,?,'SUCCEEDED','LATEST_UPDATED',?,?,?,?,?,?)",
                    (run_id,task,report_date,digest(manifest),'data/'+mkey,digest(config),utcnow(),utcnow(),seq))
                root_id='run:'+run_id
                db.execute('INSERT INTO retention_roots(root_id,run_id,retention_class,protection_reason) VALUES(?,?,?,?)',(root_id,run_id,retention_class,protection_reason))
                for item in entries:
                    db.execute('INSERT OR IGNORE INTO blobs(hash,path,size,format,schema_version,schema_hash,created_at) VALUES(?,?,?,?,?,?,?)',
                        (item['hash'],item['path'],item['size'],item['format'],item['schema_version'],item['schema_hash'],utcnow()))
                    aid=digest([run_id,item['logical_name']])
                    db.execute('INSERT INTO artifacts VALUES(?,?,?,3)',(aid,item['hash'],item['logical_name']))
                    db.execute('INSERT INTO artifact_refs VALUES(?,?,?)',(root_id,aid,item['logical_name']))
                self._facts(db,run_id,facts)
                db.execute('INSERT OR REPLACE INTO store_meta VALUES(?,?)',('latest:'+task,run_id))
                self.catalog.audit('PUBLISH_STAGE',run_id,{'identity':identity,'files':len(entries)},db=db)
            if fault:fault('DB_COMMITTED');fault('RUN_SUCCEEDED');fault('LATEST_UPDATED')
            return {i['logical_name']:self._path(i['path']) for i in entries}

    def manifest(self,run_id):
        rows=self.rows('SELECT manifest_path,manifest_hash FROM runs WHERE run_id=?',(run_id,))
        if not rows or not rows[0]['manifest_path']:raise KeyError(run_id)
        value=json.loads(self._path(rows[0]['manifest_path']).read_bytes())
        if digest(value)!=rows[0]['manifest_hash']:raise StorageIntegrityError('运行清单哈希不一致')
        return value

    def _verify_manifest(self,manifest):
        for item in manifest['files']:
            p=self._path(item['path'])
            if not p.exists() or bytes_digest(p.read_bytes())!=item['hash']:
                raise StorageIntegrityError(f"制品损坏: {item['logical_name']}")

    def _recover_one(self,manifest,fault=None):
        self._verify_manifest(manifest)
        # A v3 publish is one atomic database commit, so no split pointer repair exists.
        return {i['logical_name']:self._path(i['path']) for i in manifest['files']}

    def _latest(self,task):
        rows=self.rows("SELECT run_id FROM runs WHERE task=? AND status='SUCCEEDED' AND retired_at IS NULL ORDER BY commit_seq DESC LIMIT 1",(task,))
        if rows:self.tx(lambda db:db.execute('INSERT OR REPLACE INTO store_meta VALUES(?,?)',('latest:'+task,rows[0]['run_id'])))

    def retire(self,run_id):
        if self.rows("SELECT 1 FROM store_meta WHERE key LIKE 'latest:%' AND value=?",(run_id,)):
            raise ValueError('最新正式运行不可退役')
        return super().retire(run_id)

    def gc(self,*,apply=False):
        # Source aliases, audit documents and active run pins must not be deleted by legacy GC.
        from .数据生命周期_v3 import cleanup
        return cleanup(self.catalog,apply=apply)
