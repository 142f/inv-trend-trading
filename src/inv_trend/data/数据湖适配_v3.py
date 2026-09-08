"""DataLake v2 业务合同在统一数据库目录上的适配；不复制行情和 JSON 指针。"""
from __future__ import annotations
import io
import json
from pathlib import Path
from uuid import uuid4

from inv_trend.storage.结构化存储_v3 import UnifiedStore, CatalogPath, StoreError, utcnow
from .storage import DataLake, require_parquet, _validated_version, _EXPECTED_VERSION_UNSET
from .integrity import json_bytes, sha256_file
from .models import DataLineageError


class DatabaseDataLake(DataLake):
    def __init__(self, root='data'):
        self.store=UnifiedStore(root)
        self.root=CatalogPath(self.store.root)
        self.catalog_path=self.store.path
        self._catalog_engine='sqlite'

    def _connect(self):
        return self.store.connect()

    def _resolve_root_relative(self, raw):
        return CatalogPath(self.store.root/self.store.key(raw))

    def write_json(self,payload,relative,*,idempotent=False):
        return self.store.put_document(relative,json_bytes(payload),idempotent=idempotent)

    def write_frame(self,frame,relative,*,immutable=True):
        require_parquet()
        key=self.store.key(relative)
        previous=self.store.rows('SELECT object_id FROM data_aliases WHERE logical_path=?',(key,))
        if previous:
            if not immutable:raise FileExistsError(key)
            import pandas as pd
            from .integrity import frame_hash
            old=pd.read_parquet(self.store.resolve(key))
            if frame_hash(old)!=frame_hash(frame):raise DataLineageError('不可变Parquet内容冲突')
            return CatalogPath(self.root/key)
        out=io.BytesIO()
        frame.to_parquet(out,index=False,engine='pyarrow',compression='zstd')
        from inv_trend.storage.数据迁移_v3 import _stage
        info={'rows_count':len(frame),'schema':{str(k):str(v) for k,v in frame.dtypes.items()}}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            path=self.store.put_object(key,out.getvalue(),stage=_stage(key),info=info,db=db)
            if key.startswith(('curated/','legacy/')) and '/version=' in key and key.endswith('/bars.parquet'):
                parts=dict(x.split('=',1) for x in key.split('/') if '=' in x)
                inst=parts['instrument'];version=parts['version'];tf=parts['timeframe']
                symbol=str(frame.symbol.iloc[0]) if 'symbol' in frame and len(frame) else inst.split('.')[0]
                asset=parts.get('asset_class','unknown');channel='legacy' if asset=='legacy' or key.startswith('legacy/') else 'curated'
                status=str(frame.quality_status.iloc[0]) if 'quality_status' in frame and len(frame) else 'UNKNOWN'
                db.execute('INSERT OR IGNORE INTO instruments_v3 VALUES(?,?,?,?,?,?)',(inst,symbol,asset,'UTC',None,'SOURCE_UNVERIFIED'))
                db.execute('INSERT INTO dataset_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (version,inst,symbol,tf,channel,key,None,None,None,None,len(frame),
                     str(frame.timestamp.min()) if 'timestamp' in frame else None,
                     str(frame.timestamp.max()) if 'timestamp' in frame else None,status,utcnow(),None))
        return path

    def write_raw_payload(self,payload,provider,instrument_id,request_id,suffix='.json'):
        from .storage import _safe
        key=f'raw/provider={provider}/instrument={_safe(instrument_id)}/request_date={utcnow()[:10]}/{request_id}{suffix}'
        return self.store.put_object(key,payload,stage='raw')

    def log_event(self,payload):
        self.store.audit('INGEST',str(payload.get('run_id','')),payload)
        return self.store.path

    def current_version(self,symbol,timeframe):
        rows=self.store.rows('''SELECT h.*,v.artifact_path,v.manifest_path FROM dataset_heads h
            JOIN dataset_versions v USING(version) WHERE h.symbol=? AND h.timeframe=? AND h.channel='curated' ''',
            (symbol.upper(),timeframe.upper()))
        if not rows:return None
        r=rows[0];p={'version':r['version'],'run_id':r['run_id'],'path':r['artifact_path'],'channel':'curated'}
        if r['manifest_path']:p['dataset_manifest_path']=r['manifest_path']
        return p

    def list_versions(self,symbol,timeframe):
        return [r['version'] for r in self.store.rows("SELECT version FROM dataset_versions WHERE symbol=? AND timeframe=? AND channel='curated' ORDER BY version",(symbol.upper(),timeframe.upper()))]

    def curated_version_path(self,symbol,timeframe,version):
        version=_validated_version(version)
        rows=self.store.rows("SELECT artifact_path FROM dataset_versions WHERE symbol=? AND timeframe=? AND version=? AND channel='curated'",(symbol.upper(),timeframe.upper(),version))
        if not rows:raise FileNotFoundError(f'{symbol}/{timeframe}/{version}')
        return CatalogPath(self.root/rows[0]['artifact_path'])

    def read_legacy_bars(self,symbol,timeframe):
        require_parquet()
        import pandas as pd
        rows=self.store.rows("SELECT v.artifact_path FROM dataset_heads h JOIN dataset_versions v USING(version) WHERE h.symbol=? AND h.timeframe=? AND h.channel='legacy'",(symbol.upper(),timeframe.upper()))
        if not rows:raise FileNotFoundError(f'no legacy bars for {symbol}/{timeframe}')
        frame=pd.read_parquet(self.store.resolve(rows[0]['artifact_path']))
        frame['timestamp']=pd.to_datetime(frame['timestamp'],utc=True)
        return frame.sort_values('timestamp').drop_duplicates('timestamp',keep='last')

    def activate_curated(self,symbol,timeframe,version,run_id,path,*,channel='curated',
                         dataset_manifest_path=None,expected_current_version=_EXPECTED_VERSION_UNSET,_rollback_context=None):
        symbol,timeframe=symbol.upper(),timeframe.upper()
        key=self.store.key(path)
        # All objects/documents must be durable before the single current-head transaction.
        if not self._resolve_root_relative(key).exists():raise DataLineageError('不能激活缺失的行情对象')
        manifest=None;manifest_key=None;quality_key=None
        if dataset_manifest_path is not None:
            manifest_key=self.store.key(dataset_manifest_path)
            manifest=self.store.read_document(manifest_key)
            if (manifest.get('dataset_version')!=version or manifest.get('symbol')!=symbol or manifest.get('timeframe')!=timeframe):
                raise DataLineageError('Manifest版本、品种或周期不一致')
            if sha256_file(self._resolve_root_relative(key))!=manifest['curated_sha256'] or sha256_file(self._resolve_root_relative(manifest['curated_path']))!=manifest['curated_sha256']:
                raise DataLineageError('激活行情哈希不一致')
            quality_key=self.store.key(manifest['quality_report_path'])
            if sha256_file(self._resolve_root_relative(quality_key))!=manifest['quality_report_sha256']:
                raise DataLineageError('激活质量报告哈希不一致')
            quality=self.store.read_document(quality_key)
            if (quality.get('dataset_version',quality.get('run_id'))!=version or
                str(quality.get('symbol','')).upper()!=symbol or str(quality.get('timeframe','')).upper()!=timeframe or
                quality.get('actual_bars')!=manifest.get('row_count')):
                raise DataLineageError('质量报告版本、品种、周期或行数与发布合同不一致')
        if channel=='curated' and not manifest:
            raise DataLineageError('v3正式发布必须提供完整数据Manifest与质量报告')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT version,run_id,revision FROM dataset_heads WHERE symbol=? AND timeframe=? AND channel=?',(symbol,timeframe,channel)).fetchone()
            actual=old['version'] if old else None
            r=db.execute('SELECT * FROM dataset_versions WHERE version=?',(version,)).fetchone()
            if not r or r['symbol']!=symbol or r['timeframe']!=timeframe or r['channel']!=channel:
                raise DataLineageError('版本索引与发布合同不一致')
            if r['artifact_path']!=key:
                # The provided path may be physical; verify content identity instead.
                if sha256_file(self._resolve_root_relative(r['artifact_path']))!=sha256_file(self._resolve_root_relative(key)):
                    raise DataLineageError('发布路径与已注册版本不一致')
            if manifest and r['manifest_path'] and r['manifest_path']!=manifest_key:
                raise DataLineageError('同一不可变版本不能替换Manifest身份')
            if old and old['version']==version and old['run_id']==run_id and r['manifest_path']==manifest_key:
                return
            if expected_current_version is not _EXPECTED_VERSION_UNSET and actual!=expected_current_version:
                raise DataLineageError(f'过期发布：预期 {expected_current_version}，实际 {actual}')
            if manifest:
                if r['row_count'] is not None and manifest.get('row_count')!=r['row_count']:
                    raise DataLineageError('发布行数不一致')
                parent=manifest.get('parent_dataset_version')
                if parent and not db.execute('SELECT 1 FROM dataset_versions WHERE version=?',(parent,)).fetchone():
                    raise DataLineageError('父版本不存在')
                db.execute('UPDATE dataset_versions SET manifest_path=?,quality_path=?,publication_run_id=?,parent_version=?,rule_version=? WHERE version=?',
                           (manifest_key,quality_key,run_id,parent,manifest.get('cleaning_rule_version'),version))
            stamp=utcnow();revision=(old['revision']+1) if old else 1
            db.execute('INSERT INTO dataset_heads VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,timeframe,channel) DO UPDATE SET version=excluded.version,run_id=excluded.run_id,revision=excluded.revision,updated_at=excluded.updated_at',
                       (symbol,timeframe,channel,version,run_id,revision,stamp))
            if channel=='curated':
                db.execute('INSERT INTO current_versions VALUES(?,?,?,?,?) ON CONFLICT(symbol,timeframe) DO UPDATE SET version=excluded.version,run_id=excluded.run_id,updated_at=excluded.updated_at',(symbol,timeframe,version,run_id,stamp))
            if _rollback_context:
                db.execute('INSERT INTO rollbacks VALUES(?,?,?,?,?,?,?,?)',
                    (uuid4().hex,symbol,timeframe,actual,version,_rollback_context['actor'],_rollback_context['reason'],stamp))
            self.store.audit('ACTIVATE_VERSION',f'{symbol}/{timeframe}/{channel}',
                {'previous':actual,'target':version,'revision':revision,'rollback':_rollback_context},db=db)

    def rollback(self,symbol,timeframe,version,reason,*,actor=None):
        if not reason.strip():raise ValueError('回滚必须记录原因')
        r=self.store.rows('SELECT * FROM dataset_versions WHERE version=?',(version,))
        if not r:raise FileNotFoundError(version)
        previous=self.current_version(symbol,timeframe)
        self.activate_curated(symbol,timeframe,version,r[0]['publication_run_id'] or 'rollback',
            self.root/r[0]['artifact_path'],dataset_manifest_path=self.root/r[0]['manifest_path'] if r[0]['manifest_path'] else None,
            expected_current_version=previous['version'] if previous else None,
            _rollback_context={'actor':actor or 'unknown','reason':reason})

    def catalog(self,run_id,symbol,timeframe,source,manifest,**kwargs):
        return super().catalog(run_id,symbol,timeframe,source,self.store.key(manifest),**kwargs)

    def repair_current(self,symbol,timeframe,*,channel='curated'):
        if channel!='curated':
            raise DataLineageError('legacy通道不能修复为正式通道')
        pointer=self.current_version(symbol,timeframe)
        if not pointer:raise DataLineageError('缺失数据库current；拒绝从散落文件猜测')
        self._assert_pointer_catalog_consistent(symbol.upper(),timeframe.upper(),pointer['version'],pointer['run_id'])
        return {'action':'already_consistent','symbol':symbol,'timeframe':timeframe,'version':pointer['version']}
