"""统一 SQLite 权威、内容寻址文件和可移植逻辑引用。不改变行情字节或策略语义。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import zlib

DB_RELATIVE = Path('metadata') / '研究目录_v3.sqlite3'
STAGES = ('raw', 'processed', 'features/indicators', 'signals', 'backtests', 'reports', 'metadata')


class StoreError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=str).encode('utf-8')


def normalize_ref(raw):
    raw = str(raw).replace('\\', '/')
    while raw.startswith('./'):
        raw = raw[2:]
    if raw.startswith('data/'):
        raw = raw[5:]
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:', raw):
        raise StoreError(f'禁止绝对路径或路径穿越: {raw}')
    return path.as_posix()


class UnifiedStore:
    """只把数据库提交视为可见性边界；不可变文件先落盘，失败遗留可核验、不可误激活。"""
    def __init__(self, root='data', *, create=False, compress=True, indexes=True):
        self.root = Path(root).resolve()
        self.path = self.root / DB_RELATIVE
        self.compress = compress
        if not self.path.exists() and not create:
            raise FileNotFoundError(self.path)
        if create:
            for name in STAGES:
                (self.root / name).mkdir(parents=True, exist_ok=True)
            schema = Path(__file__).with_name('数据结构_v3.sql').read_text(encoding='utf-8')
            with self.connect() as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript(schema)
                db.execute('INSERT OR IGNORE INTO store_meta VALUES(?,?)', ('layout_version', '3'))
                db.execute('INSERT OR IGNORE INTO store_meta VALUES(?,?)', ('authority', 'sqlite'))
                db.execute('INSERT OR IGNORE INTO store_migrations VALUES(3,?,?)', (digest(schema.encode()), utcnow()))
                db.execute('PRAGMA user_version=3')
            if indexes:
                self.add_indexes()
        else:
            with self.connect(readonly=True) as db:
                row = db.execute("SELECT value FROM store_meta WHERE key='layout_version'").fetchone()
                if not row or row[0] != '3':
                    raise StoreError('不支持的数据库版本')

    @contextmanager
    def connect(self, *, readonly=False):
        if readonly:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)
        else:
            db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            if not readonly:
                db.commit()
        except BaseException:
            if not readonly:
                db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect(readonly=True) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def add_indexes(self):
        with self.connect() as db:
            db.executescript('''
            CREATE INDEX IF NOT EXISTS aliases_lookup ON data_aliases(stage,instrument_id,timeframe,dataset_version);
            CREATE INDEX IF NOT EXISTS aliases_object ON data_aliases(object_id);
            CREATE INDEX IF NOT EXISTS versions_lookup ON dataset_versions(symbol,timeframe,channel,version);
            CREATE INDEX IF NOT EXISTS docs_kind ON documents(kind,status,symbol,timeframe);
            CREATE INDEX IF NOT EXISTS docs_version ON documents(dataset_version);
            CREATE INDEX IF NOT EXISTS quality_query ON quality_assessments(backtest_suitable,status,symbol,timeframe);
            CREATE INDEX IF NOT EXISTS lineage_target ON lineage_links(reference_path,status);
            CREATE INDEX IF NOT EXISTS result_query ON result_runs(strategy_version,scope,status);
            CREATE INDEX IF NOT EXISTS result_metric_query ON result_metrics(metric_name,value);
            CREATE INDEX IF NOT EXISTS trade_query ON result_rows(instrument,event_time,side);
            ''')
            db.execute('ANALYZE')

    def key(self, path):
        raw = str(path)
        p = Path(raw)
        if p.is_absolute():
            try:
                raw = p.relative_to(self.root).as_posix()
            except ValueError as exc:
                raise StoreError(f'路径超出数据根: {path}') from exc
        return normalize_ref(raw)

    def physical(self, relative):
        path = self.root / normalize_ref(relative)
        resolved = path.resolve()
        if self.root != resolved and self.root not in resolved.parents:
            raise StoreError('物理文件越出数据根')
        return path

    def audit(self, event_type, subject, payload, *, db=None):
        if db is None:
            with self.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.audit(event_type, subject, payload, db=connection)
        prev = db.execute('SELECT event_hash FROM audit_events_v3 ORDER BY event_id DESC LIMIT 1').fetchone()
        previous = prev[0] if prev else ''
        timestamp = utcnow()
        body = canonical(payload).decode()
        h = digest(canonical([event_type, str(subject), timestamp, body, previous]))
        db.execute('INSERT INTO audit_events_v3(event_type,subject,occurred_at,payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?)',
                   (event_type, str(subject), timestamp, body, previous, h))
        return h

    def put_document(self, logical, payload, *, kind=None, idempotent=True, db=None):
        key = self.key(logical)
        data = payload if isinstance(payload, bytes) else canonical(payload)
        sha = digest(data)
        # SQL projection is derived from original bytes; never rewrites audit source payload.
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        if db is None:
            with self.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.put_document(key, data, kind=kind, idempotent=idempotent, db=connection)
        old = db.execute('SELECT sha256 FROM documents WHERE logical_path=?', (key,)).fetchone()
        if old:
            if old[0] == sha and idempotent:
                return CatalogPath(self.root / key)
            raise FileExistsError(f'不可变文档冲突: {key}')
        codec = 'zlib' if self.compress else 'identity'
        stored = zlib.compress(data, 6) if self.compress else data
        db.execute('INSERT OR IGNORE INTO document_payloads VALUES(?,?,?,?,?)', (sha, stored, codec, len(data), utcnow()))
        kind = kind or key.split('/')[0]
        status = obj.get('quality_status', obj.get('status'))
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)',
            (key, sha, kind, obj.get('run_id'), obj.get('dataset_version'), obj.get('symbol',obj.get('requested_symbol')),
             obj.get('timeframe'), status, obj.get('downloaded_at',obj.get('created_at')), utcnow()))
        if kind in ('quality_reports', 'ingestion_reports'):
            cols = ['actual_bars','theoretical_bars','missing_count','missing_ratio','duplicate_count',
                    'ohlc_anomaly_count','quality_score','backtest_suitable','calendar_valid','timezone_valid']
            db.execute('INSERT INTO quality_assessments VALUES('+','.join('?'*17)+')',
                (key,obj.get('dataset_version'),obj.get('run_id'),obj.get('symbol'),obj.get('timeframe'),status,
                 *(obj.get(k) for k in cols),obj.get('created_at')))
            for i, val in enumerate(obj.get('missing_intervals', [])):
                if isinstance(val, dict):
                    db.execute('INSERT INTO quality_intervals VALUES(?,?,?,?,?,?,?,?)',
                        (key,i,val.get('start'),val.get('end'),val.get('classification'),
                         val.get('expected_bars'),val.get('observed_bars'),val.get('calendar_id')))
        if key.endswith('/实验协议冻结.json') and isinstance(obj.get('protocol'), dict):
            protocol=obj['protocol'];study=key.rsplit('/',1)[0]
            db.execute('INSERT INTO studies_v3 VALUES(?,?,?,?,?,?,?,?,?)',
                (study,key,digest(canonical(protocol)),protocol['dataset_sha256'],obj.get('source_sha256'),
                 protocol.get('strategy_version'),int(bool(protocol.get('production_enabled'))),'FROZEN',utcnow()))
        if '/参数锁定/' in key and 'parameters' in obj:
            study=key.split('/参数锁定/')[0]
            ph=digest(canonical(obj['parameters']))
            db.execute('INSERT OR IGNORE INTO result_parameters VALUES(?,?,?)',(ph,canonical(obj['parameters']).decode(),utcnow()))
            if not db.execute('SELECT 1 FROM studies_v3 WHERE study_id=?',(study,)).fetchone():
                raise StoreError('参数锁必须关联先于它发布的冻结研究协议')
            def stamp(value):
                t=datetime.fromisoformat(str(value).replace('Z','+00:00'))
                if t.tzinfo is None:raise StoreError('窗口时间必须包含时区')
                return t.astimezone(timezone.utc).isoformat()
            bounds=[stamp(obj[k]) for k in ('train_start','train_end','test_start','test_end_exclusive')]
            if not bounds[0]<bounds[1]<bounds[2]<bounds[3]:raise StoreError('滚动窗口时间顺序错误')
            db.execute('INSERT INTO wf_windows_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (study,obj.get('iteration',key.split('/')[-2]),obj['window_id'],*bounds,obj['candidate_id'],ph,sha,key))
            for rank in obj.get('ranking',[]):
                db.execute('INSERT INTO candidate_rankings_v3 VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (study,obj.get('iteration',key.split('/')[-2]),obj['window_id'],rank['candidate_id'],rank.get('score'),int(bool(rank.get('eligible'))),
                     rank.get('positive_inner_ratio'),rank.get('inner_drawdown'),rank.get('trade_count'),rank.get('complexity_rank')))
        return CatalogPath(self.root / key)

    def document_bytes(self, logical):
        key = self.key(logical)
        with self.connect(readonly=True) as db:
            r = db.execute('SELECT p.* FROM documents d JOIN document_payloads p ON d.sha256=p.sha256 WHERE d.logical_path=?', (key,)).fetchone()
        if not r:
            raise FileNotFoundError(key)
        data = zlib.decompress(r['payload']) if r['codec'] == 'zlib' else bytes(r['payload'])
        if len(data) != r['byte_size'] or digest(data) != r['sha256']:
            raise StoreError(f'文档损坏: {key}')
        return data

    def read_document(self, logical):
        return json.loads(self.document_bytes(logical))

    def put_object(self, logical, data, *, stage='processed', info=None, db=None):
        key = self.key(logical)
        if db is None:
            with self.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.put_object(key,data,stage=stage,info=info,db=connection)
        h = digest(data)
        old = db.execute('SELECT object_id FROM data_aliases WHERE logical_path=?', (key,)).fetchone()
        if old:
            if old[0] != h:
                raise StoreError(f'不可变数据冲突: {key}')
            return self.resolve(key)
        info = info or {}
        existing = db.execute('SELECT * FROM data_objects WHERE object_id=?', (h,)).fetchone()
        if not existing:
            suffix = PurePosixPath(key).suffix.lower() or '.bin'
            codec = 'gzip' if self.compress and stage=='raw' and suffix in ('.json','.jsonl','.txt') else 'identity'
            encoded = gzip.compress(data,mtime=0,compresslevel=6) if codec=='gzip' else data
            stage_dir = stage + ('/indicators' if stage=='features' else '')
            relative = f'{stage_dir}/{h[:2]}/数据_{h}{suffix}' + ('.gz' if codec=='gzip' else '')
            target = self.physical(relative)
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if digest(target.read_bytes()) != digest(encoded):
                    raise StoreError('内容地址目标已损坏')
            else:
                fd, tmp = tempfile.mkstemp(prefix='暂存_',dir=target.parent)
                try:
                    with os.fdopen(fd,'wb') as out:
                        out.write(encoded);out.flush();os.fsync(out.fileno())
                    os.replace(tmp,target)
                    if hasattr(os,'O_DIRECTORY'):
                        directory_fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
                        try:os.fsync(directory_fd)
                        finally:os.close(directory_fd)
                finally:
                    if os.path.exists(tmp): os.unlink(tmp)
            schema_bytes=canonical(info.get('schema'))
            schema_id=digest(schema_bytes)
            db.execute('INSERT OR IGNORE INTO data_schemas VALUES(?,?)',(schema_id,schema_bytes.decode()))
            db.execute('INSERT INTO data_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (h,relative,suffix.lstrip('.'),codec,len(data),len(encoded),digest(encoded),stage,
                 info.get('rows_count'),schema_id,utcnow()))
        parts = dict(re.findall(r'(instrument|timeframe|year|version)=([^/]+)', key))
        db.execute('INSERT INTO data_aliases VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (key,h,stage,len(data),h,parts.get('instrument'),parts.get('timeframe'),
             int(parts['year']) if parts.get('year','').isdigit() else None,parts.get('version'),'SOURCE',utcnow()))
        return CatalogPath(self.root / key)

    def resolve(self, logical):
        key = self.key(logical)
        records = self.rows('SELECT o.path,o.codec FROM data_aliases a JOIN data_objects o ON a.object_id=o.object_id WHERE a.logical_path=?', (key,))
        if records and records[0]['codec']=='identity':
            return self.physical(records[0]['path'])
        if records or self.rows('SELECT 1 FROM documents WHERE logical_path=?',(key,)):
            return CatalogPath(self.root / key)
        return self.physical(key)

    def read_bytes(self, logical):
        key = self.key(logical)
        rows = self.rows('SELECT o.* FROM data_aliases a JOIN data_objects o ON a.object_id=o.object_id WHERE a.logical_path=?',(key,))
        if not rows:
            return self.document_bytes(key)
        r=rows[0]; encoded=self.physical(r['path']).read_bytes()
        if digest(encoded)!=r['stored_sha256']:
            raise StoreError('物理对象哈希不一致')
        data=gzip.decompress(encoded) if r['codec']=='gzip' else encoded
        if digest(data)!=r['object_id']:
            raise StoreError('原始对象哈希不一致')
        return data

    def checkpoint(self, vacuum=False):
        with self.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if vacuum:
                db.execute('VACUUM')
            db.execute('PRAGMA optimize')

    def validate(self, *, source=None):
        issues=[]
        with self.connect(readonly=True) as db:
            integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
            foreign=[tuple(x) for x in db.execute('PRAGMA foreign_key_check')]
        if integrity!='ok' or foreign:
            issues.append({'database':integrity,'foreign_keys':foreign})
        for r in self.rows('SELECT * FROM data_objects'):
            try:
                p=self.physical(r['path']);b=p.read_bytes()
                if len(b)!=r['stored_size'] or digest(b)!=r['stored_sha256']:
                    raise StoreError('存储哈希/长度不符')
                raw=gzip.decompress(b) if r['codec']=='gzip' else b
                if len(raw)!=r['original_size'] or digest(raw)!=r['object_id']:
                    raise StoreError('内容哈希/长度不符')
            except Exception as exc:
                issues.append({'object':r['object_id'],'error':str(exc)})
        for r in self.rows('SELECT logical_path FROM documents'):
            try:self.document_bytes(r['logical_path'])
            except Exception as exc:issues.append({'document':r['logical_path'],'error':str(exc)})
        previous=''
        for r in self.rows('SELECT * FROM audit_events_v3 ORDER BY event_id'):
            actual=digest(canonical([r['event_type'],r['subject'],r['occurred_at'],r['payload_json'],previous]))
            if r['previous_hash']!=previous or actual!=r['event_hash']:
                issues.append({'audit_event':r['event_id']})
            previous=r['event_hash']
        mismatch=self.rows('''SELECT h.symbol,h.timeframe,h.version,c.version catalog_version
            FROM dataset_heads h LEFT JOIN current_versions c ON h.symbol=c.symbol AND h.timeframe=c.timeframe
            WHERE h.channel='curated' AND (c.version IS NULL OR h.version<>c.version OR h.run_id<>c.run_id)''')
        if mismatch:issues.append({'current_head_catalog_mismatch':mismatch})
        failures=self.rows("SELECT value FROM store_meta WHERE key='migration_status' AND value='FAILED'")
        if failures:issues.append({'migration_status':'FAILED'})
        if source:
            source=Path(source)
            listed={r['source_path'] for r in self.rows('SELECT source_path FROM source_inventory')}
            actual={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
            if listed!=actual:issues.append({'source_inventory_missing':sorted(actual-listed),'unexpected_sources':sorted(listed-actual)})
            for r in self.rows('SELECT * FROM source_inventory'):
                try:
                    b=(source/r['source_path']).read_bytes()
                    if len(b)!=r['source_size'] or digest(b)!=r['source_sha256']:
                        raise StoreError('来源已变化')
                    if r['target_kind'] in ('document','object') and self.read_bytes(r['source_path'])!=b:
                        raise StoreError('迁移字节不一致')
                except Exception as exc:issues.append({'source':r['source_path'],'error':str(exc)})
        return {'ok':not issues,'issues':issues,'objects':self.rows('SELECT count(*) n FROM data_objects')[0]['n'],
                'documents':self.rows('SELECT count(*) n FROM documents')[0]['n'],'integrity':integrity}


# 只读旧路径适配器：文档由数据库提供，Parquet 映射到真实文件，绝不恢复散落 JSON。
# 业务代码应优先调用 UnifiedStore；此适配器仅维持 v2 的 Path 读取合同。
class CatalogPath(type(Path())):
    def _store(self):
        p=Path(str(self))
        for root in (p,*p.parents):
            if (root/DB_RELATIVE).is_file():
                return UnifiedStore(root)
        return None

    def _dynamic_pointer(self, store):
        try:key=store.key(self)
        except StoreError:return None
        match=re.fullmatch(r'curated/symbol=([^/]+)/timeframe=([^/]+)/(current|legacy_current)\.json',key)
        if not match:return None
        symbol,tf,token=match.groups();channel='legacy' if token=='legacy_current' else 'curated'
        rows=store.rows('SELECT h.*,v.artifact_path,v.manifest_path FROM dataset_heads h JOIN dataset_versions v USING(version) WHERE h.symbol=? AND h.timeframe=? AND h.channel=?',(symbol,tf,channel))
        if not rows:return None
        r=rows[0];value={'version':r['version'],'run_id':r['run_id'],'path':r['artifact_path'],'channel':channel}
        if r['manifest_path']:value['dataset_manifest_path']=r['manifest_path']
        return canonical(value)

    def __fspath__(self):
        raw=str(self);store=self._store()
        if store:
            try:
                key=store.key(raw)
                rows=store.rows('SELECT o.path,o.codec FROM data_aliases a JOIN data_objects o ON a.object_id=o.object_id WHERE a.logical_path=?',(key,))
                if rows and rows[0]['codec']=='identity':return str(store.physical(rows[0]['path']))
            except StoreError:pass
        return raw

    def read_bytes(self):
        store=self._store()
        if store:
            pointer=self._dynamic_pointer(store)
            if pointer is not None:return pointer
            try:return store.read_bytes(self)
            except FileNotFoundError:pass
            key=store.key(self)
            # Database is authoritative; an old unregistered metadata file may not shadow it.
            if key.split('/')[0] in ('manifests','dataset_manifests','quality_reports','ingestion_reports','reviews','normalized','curated'):
                raise FileNotFoundError(f'旧路径未在数据库登记: {key}')
            return store.physical(key).read_bytes()
        return Path(str(self)).read_bytes()

    def read_text(self,encoding=None,errors=None,**kwargs):
        return self.read_bytes().decode(encoding or 'utf-8', errors or 'strict')

    def open(self,mode='r',buffering=-1,encoding=None,errors=None,newline=None,**kwargs):
        if mode not in ('r','rb','rt'):
            return Path(str(self)).open(mode,buffering,encoding,errors,newline,**kwargs)
        data=self.read_bytes()
        return io.BytesIO(data) if 'b' in mode else io.StringIO(data.decode(encoding or 'utf-8',errors or 'strict'))

    def exists(self,**kwargs):
        store=self._store()
        if not store:return Path(str(self)).exists()
        try:key=store.key(self)
        except StoreError:return Path(str(self)).exists()
        if self._dynamic_pointer(store) is not None:return True
        if store.rows('SELECT 1 FROM documents WHERE logical_path=? UNION ALL SELECT 1 FROM data_aliases WHERE logical_path=? LIMIT 1',(key,key)):
            return True
        if key.endswith(('.csv.gz', '.csv')):
            from .研究结果_v3 import ResultRepository
            p=PurePosixPath(key)
            return ResultRepository(store).has_table(p.parent,p.name.removesuffix('.csv.gz').removesuffix('.csv'))
        if key.split('/')[0] in ('manifests','dataset_manifests','quality_reports','ingestion_reports','reviews','normalized','curated'):
            return False
        return store.physical(key).exists()

    def is_file(self,**kwargs):
        return self.exists() and not Path(str(self)).is_dir()

    def glob(self,pattern,**kwargs):
        found={str(p):CatalogPath(p) for p in Path(str(self)).glob(pattern,**kwargs)}
        store=self._store()
        if store:
            try:
                prefix='' if Path(str(self))==store.root else store.key(self)+'/'
                allrows=store.rows('SELECT logical_path FROM documents WHERE logical_path LIKE ? UNION SELECT logical_path FROM data_aliases WHERE logical_path LIKE ?', (prefix+'%',prefix+'%'))
                if prefix.split('/')[0] in ('manifests','dataset_manifests','quality_reports','ingestion_reports','reviews','normalized','curated'):
                    found={}
                for head in store.rows('SELECT symbol,timeframe,channel FROM dataset_heads'):
                    token='legacy_current' if head['channel']=='legacy' else 'current'
                    logical=f"curated/symbol={head['symbol']}/timeframe={head['timeframe']}/{token}.json"
                    if logical.startswith(prefix):allrows.append({'logical_path':logical})
                # Match each segment, not fnmatch's slash-spanning wildcard.
                token=pattern.split('/')
                for r in allrows:
                    tail=r['logical_path'][len(prefix):].split('/')
                    if len(tail)>=len(token) and all(fnmatch.fnmatchcase(a,b) for a,b in zip(tail,token)):
                        key=prefix+'/'.join(tail[:len(token)])
                        p=CatalogPath(store.root/key);found[str(p)]=p
            except StoreError:pass
        yield from (found[k] for k in sorted(found))

    def resolve(self,strict=False):
        return CatalogPath(Path(str(self)).resolve(strict=False))

    def absolute(self):
        return CatalogPath(Path(str(self)).absolute())

    def unlink(self,missing_ok=False):
        if not Path(str(self)).exists() and self.exists():
            raise StoreError('不能通过旧文件 API 删除数据库审计记录')
        return Path(str(self)).unlink(missing_ok=missing_ok)
