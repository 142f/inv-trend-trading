"""统一存储契约。真实 SQLite 和原始字节测试；Parquet解码依赖另行显式验收。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import sqlite3
import pytest
from inv_trend.storage.结构化存储_v3 import UnifiedStore, CatalogPath, StoreError, DB_RELATIVE

@pytest.fixture
def store(tmp_path):
    return UnifiedStore(tmp_path/'data',create=True)


def test_document_byte_exact(store):
    b=b'{\n "a": 1, "b":true\n}'
    p=store.put_document('manifests/中文.json',b)
    assert p.read_bytes()==b and p.exists()
    assert not Path(str(p)).exists()
    assert store.document_bytes('data\\manifests\\中文.json')==b
    assert store.validate()['ok']


def test_immutable_retry_conflict(store):
    store.put_document('metadata/a.json',{'a':1})
    store.put_document('metadata/a.json',{'a':1})
    with pytest.raises(FileExistsError):store.put_document('metadata/a.json',{'a':2})
    assert store.read_document('metadata/a.json')=={'a':1}


@pytest.mark.parametrize('key',['../a','/tmp/a','C:\\bad.json','a/../../b'])
def test_path_security(store,key):
    with pytest.raises(StoreError):store.put_document(key,{})


def test_blob_deduplication(store):
    p=store.put_object('normalized/a.parquet',b'unchanged-original-bytes')
    q=store.put_object('normalized/b.parquet',b'unchanged-original-bytes')
    assert p.read_bytes()==q.read_bytes()
    assert len(store.rows('SELECT * FROM data_objects'))==1
    assert len(store.rows('SELECT * FROM data_aliases'))==2


def test_raw_lossless_compression(store):
    b=b'raw provider original '+b' '*10000
    p=store.put_object('raw/test.json',b,stage='raw')
    assert p.read_bytes()==b
    r=store.rows('SELECT * FROM data_objects')[0]
    assert r['stored_size']<r['original_size']
    assert r['object_id']==hashlib.sha256(b).hexdigest()


def test_doc_delete_forbidden(store):
    p=store.put_document('metadata/a.json',{})
    with pytest.raises(StoreError):p.unlink()
    assert p.exists()


def test_transaction_rollback(store):
    with pytest.raises(RuntimeError):
        with store.connect() as db:
            store.put_document('metadata/not_committed.json',{},db=db)
            raise RuntimeError('fault')
    assert not (CatalogPath(store.root)/'metadata/not_committed.json').exists()


def test_fk_enabled(store):
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as db:
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)',('x','f'*64,'test',None,None,None,None,None,None,'now'))


def test_audit_chain_immutable(store):
    store.audit('TEST','one',{'i':1});store.audit('TEST','two',{'i':2})
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as db:db.execute("UPDATE audit_events_v3 SET subject='x'")
    assert store.validate()['ok']


def test_parallel_writers(store):
    def f(i):
        local=UnifiedStore(store.root)
        local.put_document(f'metadata/{i}.json',{'i':i})
        local.audit('THREAD',str(i),{'i':i})
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(f,range(24)))
    assert len(store.rows('SELECT * FROM documents'))==24
    assert store.validate()['ok']


def test_glob_keeps_path_segments(store):
    store.put_document('manifests/a.json',{})
    store.put_document('manifests/deep/b.json',{})
    root=CatalogPath(store.root)
    assert [p.name for p in (root/'manifests').glob('*.json')]==['a.json']
    assert [p.name for p in (root/'manifests').glob('*/*.json')]==['b.json']


def test_database_lake_selected(store):
    from inv_trend.data.storage import DataLake
    from inv_trend.data.数据湖适配_v3 import DatabaseDataLake
    lake=DataLake(store.root)
    assert isinstance(lake,DatabaseDataLake)
    assert lake.catalog_path==store.path
    lake.write_json({'value':1},Path('manifests/a.json'))
    assert json.loads((lake.root/'manifests/a.json').read_text())=={'value':1}
    assert not (Path(str(lake.root))/'catalog.sqlite3').exists()


def test_missing_document_fails(store):
    with pytest.raises(FileNotFoundError):store.document_bytes('quality_reports/missing.json')


def test_binary_path_compatible(store):
    import os
    p=store.put_object('normalized/a.parquet',b'PAR1examplePAR1')
    assert Path(os.fspath(p)).read_bytes()==b'PAR1examplePAR1'
    assert str(p).endswith('normalized/a.parquet')


def test_source_payload_corruption_detected(store):
    store.put_document('manifests/a.json',{})
    with store.connect() as db:db.execute("UPDATE document_payloads SET payload=x'0000'")
    assert not store.validate()['ok']


def test_object_corruption_detected(store):
    store.put_object('normalized/a.parquet',b'original')
    r=store.rows('SELECT * FROM data_objects')[0]
    store.physical(r['path']).write_bytes(b'changed')
    assert not store.validate()['ok']


def test_pyarrow_explicit_requirement():
    from inv_trend.data.storage import require_parquet
    import importlib.util
    if importlib.util.find_spec('pyarrow') is None:
        with pytest.raises(RuntimeError,match='Parquet support requires'):require_parquet()
    else:require_parquet()


def test_result_table_exact_roundtrip(store):
    import pandas as pd
    from inv_trend.storage.研究结果_v3 import ResultRepository
    frames={'权益曲线':pd.Series([100.,101.,99.],index=pd.date_range('2020-01-01',periods=3,tz='UTC'),name='equity'),
            '交易记录':pd.DataFrame({'symbol':['BTC','ETH'],'pnl':[1.25,-.7],'side':[1,-1]})}
    repo=ResultRepository(store)
    repo.publish('backtests/test',frames,{'metrics':{'total_return':-.01},'parameters':{'risk':.005}})
    for name,frame in frames.items():
        loaded=repo.read_table('backtests/test',name)
        (pd.testing.assert_series_equal if isinstance(frame,pd.Series) else pd.testing.assert_frame_equal)(frame,loaded)
    assert repo.validate()['tables']==2
    assert not list(store.root.rglob('*.csv*'))


def test_empty_result_table(store):
    import pandas as pd
    from inv_trend.storage.研究结果_v3 import ResultRepository
    repo=ResultRepository(store);frame=pd.DataFrame(columns=['symbol','pnl'])
    repo.publish('backtests/empty',{'交易记录':frame},{})
    pd.testing.assert_frame_equal(frame,repo.read_table('backtests/empty','交易记录'))


def test_unified_stage_store(store):
    from inv_trend.storage.阶段制品 import StageStore
    from inv_trend.storage.统一仓库适配_v3 import DatabaseStorage
    stage=StageStore(store.root.parent)
    assert isinstance(stage.storage,DatabaseStorage)
    sid=stage.publish('data',{'rows':[1,2,3]},parameters={'f':1})
    _,body=stage.load(sid)
    assert body=={'rows':[1,2,3]}
    assert not (store.root.parent/'outputs/系统/运行结果.sqlite3').exists()
    assert stage.storage.verify()['ok']


def test_stage_fault_is_atomic(store):
    from inv_trend.storage.仓库 import Storage
    repo=Storage(store.root.parent)
    def fail(phase):
        if phase=='FILES_PUBLISHED':raise RuntimeError('fault')
    with pytest.raises(RuntimeError):repo.publish('test','r1','date',{'a.json':b'{}'},fault=fail)
    assert not repo.rows('SELECT * FROM runs')
    assert not store.rows('SELECT * FROM documents')
    repo.publish('test','r1','date',{'a.json':b'{}'})
    assert repo.verify()['ok']


def test_run_conflict_cannot_overwrite(store):
    import pandas as pd
    from inv_trend.storage.研究结果_v3 import ResultRepository
    repo=ResultRepository(store)
    repo.publish('backtests/immutable',{'x':pd.DataFrame({'x':[1]})},{})
    with pytest.raises(StoreError):repo.publish('backtests/immutable',{'x':pd.DataFrame({'x':[2]})},{})
    assert repo.read_table('backtests/immutable','x').iloc[0,0]==1


def test_fixed_research_market_roundtrip(store):
    from inv_trend.storage.研究结果_v3 import import_market_csv,load_market_frame
    import pandas as pd,io
    b=b'date,symbol,open,high,low,close,volume,spread,source,timeframe\n2020-01-01 00:00:00+00:00,A,1.1,1.2,1.0,1.15,100.0,0.0,s,D1\n'
    h=hashlib.sha256(b).hexdigest()
    import_market_csv(store,b,'test',h)
    pd.testing.assert_frame_equal(pd.read_csv(io.BytesIO(b),float_precision='round_trip'),load_market_frame(store,h))


def test_gc_protects_source_and_only_removes_old_temporary(store):
    import os,time
    from inv_trend.storage.数据生命周期_v3 import cleanup
    store.put_object('raw/market.bin',b'retain',stage='raw')
    orphan=store.root/'processed/暂存_orphan';orphan.write_bytes(b'orphan')
    old=time.time()-10*86400;os.utime(orphan,(old,old))
    rows=cleanup(store)
    assert len(rows)==1 and orphan.exists()
    cleanup(store,apply=True)
    assert not orphan.exists() and store.read_bytes('raw/market.bin')==b'retain'


def test_schema_dedup(store):
    for i in range(5):store.put_object(f'normalized/{i}.bin',str(i).encode(),info={'schema':{'x':'float64'}})
    assert len(store.rows('SELECT * FROM data_schemas'))==1


def test_database_backup_relocatable(store,tmp_path):
    import shutil
    from inv_trend.storage.数据生命周期_v3 import backup_database
    store.put_document('metadata/a.json',{'a':1});store.put_object('raw/a.bin',b'aaa',stage='raw')
    target=tmp_path/'moved';shutil.copytree(store.root,target)
    path=target/DB_RELATIVE;path.unlink()
    backup_database(store,path)
    other=UnifiedStore(target)
    assert other.read_document('metadata/a.json')=={'a':1}
    assert other.read_bytes('raw/a.bin')==b'aaa'
    assert other.validate()['ok']


def test_parameter_lock_rejects_future_train(store):
    protocol={'dataset_sha256':'a'*64,'strategy_version':'test','production_enabled':False}
    store.put_document('backtests/s/实验协议冻结.json',{'protocol':protocol})
    lock={'iteration':'i','window_id':'w','parameters':{},'candidate_id':'c',
          'train_start':'2020-01-01T00:00:00+00:00','train_end':'2022-01-01T00:00:00+00:00',
          'test_start':'2021-01-01T00:00:00+00:00','test_end_exclusive':'2023-01-01T00:00:00+00:00'}
    with pytest.raises(StoreError):store.put_document('backtests/s/参数锁定/i/w.json',lock)
    assert not store.rows('SELECT * FROM wf_windows_v3')


def test_parameter_lock_before_protocol_fails(store):
    with pytest.raises(StoreError):
        store.put_document('backtests/s/参数锁定/i/w.json',{'parameters':{},'window_id':'w'})


def test_legacy_shadow_cannot_override_database(store):
    p=store.put_document('quality_reports/a.json',{'symbol':'A','actual_bars':1})
    shadow=Path(str(p));shadow.parent.mkdir(parents=True);shadow.write_text('{"bad":true}')
    assert p.read_bytes()==store.document_bytes('quality_reports/a.json')
    assert 'bad' not in json.loads(p.read_bytes())


def test_legacy_object_shadow_cannot_override_database(store):
    import os
    p=store.put_object('normalized/a.parquet',b'correct')
    shadow=Path(str(p));shadow.parent.mkdir(parents=True);shadow.write_bytes(b'wrong')
    assert p.read_bytes()==b'correct' and Path(os.fspath(p)).read_bytes()==b'correct'


def test_exact_empty_range_columns_and_object_nulls(tmp_path):
    import numpy as np
    import pandas as pd
    from inv_trend.storage.研究结果_v3 import ResultRepository
    store=UnifiedStore(tmp_path/'data',create=True);repo=ResultRepository(store)
    frames={'空表':pd.DataFrame(),'匿名序列':pd.Series([1.,2.]),'缺失':pd.DataFrame({'a':pd.Series([None,np.nan,pd.NA,pd.NaT,'x'],dtype='object')})}
    repo.publish('backtests/边界',frames,{})
    for key,frame in frames.items():
        got=repo.read_table('backtests/边界',key)
        if isinstance(frame,pd.Series):pd.testing.assert_series_equal(frame,got)
        else:pd.testing.assert_frame_equal(frame,got)
    got=repo.read_table('backtests/边界','缺失')
    assert got.a.iloc[0] is None and got.a.iloc[2] is pd.NA and got.a.iloc[3] is pd.NaT


def _storage_version(store,version,parent=None):
    # Binary storage contract only; deliberately NOT presented as Parquet decoding.
    from inv_trend.storage.结构化存储_v3 import digest,canonical,utcnow
    key=f'processed/TEST/D1/{version}/source.bin';data=version.encode()
    store.put_object(key,data,info={'rows_count':1})
    qkey=f'quality_reports/{version}.json'
    q={'dataset_version':version,'symbol':'TEST','timeframe':'D1','actual_bars':1,'quality_status':'RESEARCH_ONLY'}
    store.put_document(qkey,q)
    manifest={'dataset_version':version,'symbol':'TEST','timeframe':'D1','curated_path':key,
              'curated_sha256':digest(data),'quality_report_path':qkey,
              'quality_report_sha256':digest(canonical(q)),'row_count':1,'parent_dataset_version':parent}
    mkey=f'dataset_manifests/{version}.json';store.put_document(mkey,manifest)
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO instruments_v3 VALUES('TEST','TEST','test','UTC',NULL,'SOURCE_UNVERIFIED')")
        db.execute('INSERT INTO dataset_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (version,'TEST','TEST','D1','curated',key,parent,'run-'+version,mkey,qkey,1,None,None,'RESEARCH_ONLY',utcnow(),None))
    return key,mkey


def test_activation_idempotent_stale_cas_and_atomic_rollback(store,monkeypatch):
    from inv_trend.data.storage import DataLake
    from inv_trend.data.models import DataLineageError
    lake=DataLake(store.root)
    p1,m1=_storage_version(store,'test-v1');p2,m2=_storage_version(store,'test-v2','test-v1')
    lake.activate_curated('TEST','D1','test-v1','run-test-v1',p1,dataset_manifest_path=m1,expected_current_version=None)
    before=store.rows('SELECT * FROM dataset_heads')
    # Exact retry succeeds even when the supplied previous token is stale.
    lake.activate_curated('TEST','D1','test-v1','run-test-v1',p1,dataset_manifest_path=m1,expected_current_version=None)
    assert store.rows('SELECT * FROM dataset_heads')==before
    with pytest.raises(DataLineageError):
        lake.activate_curated('TEST','D1','test-v2','run-test-v2',p2,dataset_manifest_path=m2,expected_current_version=None)
    original_audit=lake.store.audit
    def fail(*args,**kwargs):raise RuntimeError('事务提交前故障')
    monkeypatch.setattr(lake.store,'audit',fail)
    with pytest.raises(RuntimeError):
        lake.activate_curated('TEST','D1','test-v2','run-test-v2',p2,dataset_manifest_path=m2,expected_current_version='test-v1')
    assert store.rows('SELECT * FROM dataset_heads')==before
    assert store.rows('SELECT version FROM current_versions')[0]['version']=='test-v1'
    monkeypatch.setattr(lake.store,'audit',original_audit)
    lake.activate_curated('TEST','D1','test-v2','run-test-v2',p2,dataset_manifest_path=m2,expected_current_version='test-v1')
    lake.rollback('TEST','D1','test-v1','验收回滚',actor='test')
    assert lake.current_version('TEST','D1')['version']=='test-v1'
    assert store.rows('SELECT count(*) n FROM rollbacks')[0]['n']==1
    assert store.validate()['ok']


def test_sql_authority_detects_compatibility_current_drift(store):
    from inv_trend.data.storage import DataLake
    lake=DataLake(store.root);key,mkey=_storage_version(store,'test-v1')
    lake.activate_curated('TEST','D1','test-v1','run-test-v1',key,dataset_manifest_path=mkey)
    with store.connect() as db:db.execute("UPDATE current_versions SET version='bad'")
    assert not store.validate()['ok']


def test_external_registry_uses_same_database_without_binding_file(store,tmp_path):
    from inv_trend.storage.实验登记 import SQLiteRegistry
    external=tmp_path.parent/('外部导出-'+tmp_path.name)/'登记.jsonl'
    registry=SQLiteRegistry(external,root=store.root.parent)
    assert registry.store.path==store.path
    assert registry.registry_id.startswith('external:')
    assert not registry.binding.exists()
    assert registry.read()==[]


def test_reject_remigrating_v3(store,tmp_path):
    from inv_trend.storage.数据迁移_v3 import migrate
    with pytest.raises(StoreError,match='已是v3'):
        migrate(store.root,tmp_path/'another_data')


def test_existing_virtual_study_cannot_be_overwritten(store):
    from inv_trend.application.滚动验证 import run_research
    store.put_document('backtests/已有/实验协议冻结.json',{'protocol':{'dataset_sha256':'a'*64,'production_enabled':False}})
    assert not (store.root/'backtests/已有').exists()
    with pytest.raises(FileExistsError,match='研究ID已存在'):
        run_research(store.root.parent,store.root/'backtests/已有')


@pytest.mark.parametrize('field',['quality','artifact'])
def test_publication_rejects_misbound_manifest_before_commit(store,field):
    from inv_trend.data.storage import DataLake
    from inv_trend.data.models import DataLineageError
    from inv_trend.storage.结构化存储_v3 import digest,canonical
    lake=DataLake(store.root);key,mkey=_storage_version(store,'test-v1')
    manifest=store.read_document(mkey)
    if field=='quality':
        q={'dataset_version':'different','symbol':'TEST','timeframe':'D1','actual_bars':1}
        store.put_document('quality_reports/另一个.json',q)
        manifest.update(quality_report_path='quality_reports/另一个.json',quality_report_sha256=digest(canonical(q)))
    else:
        store.put_object('processed/另一个.bin',b'other')
        manifest.update(curated_path='processed/另一个.bin',curated_sha256=digest(b'other'))
    store.put_document('dataset_manifests/错误绑定.json',manifest)
    with pytest.raises(DataLineageError):
        lake.activate_curated('TEST','D1','test-v1','run-test-v1',key,dataset_manifest_path='dataset_manifests/错误绑定.json')
    assert lake.current_version('TEST','D1') is None
