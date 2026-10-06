"""Audit corruption on isolated backups, never alter the running observation."""
import importlib.util
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fault',['foreign_key','snapshot_version','delivered_receipt','incomplete_audit_tail'])
def test_checkpoint_validates_backup_corruption_and_concurrent_audit_tail(tmp_path,fault):
    from inv_trend.monitoring.事件存储 import EventStore
    from inv_trend.monitoring.执行记录 import RuntimeLedger
    from inv_trend.monitoring.扫描器 import CodeVersion
    from inv_trend.monitoring.状态机 import transition
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    from inv_trend.notifications.队列 import NotificationWorker
    from tests.test_监控状态与事务合同 import decision,facts
    from tests.test_通知事务队列合同 import Channel,NOW
    source = tmp_path/'原始隔离观察'
    store = EventStore(source/'运行'/'监控事件.sqlite3')
    RuntimeLedger(store,tmp_path/'data')
    ProcessSupervisor(RuntimeConfig(data_root=tmp_path/'data',runtime_dir=source/'运行'))
    before = store.state('BTC','D1')
    outcome = transition(before,facts(close_confirmed=True))
    code = CodeVersion.capture()
    snapshot = replace(decision(before,outcome),code_hash=code.source_hash)
    store.commit(snapshot,before,outcome.after)
    NotificationWorker(store,Channel(),clock=lambda:NOW).run_once()
    status = {'source_hash':code.source_hash,'status':'RUNNING','elapsed_seconds':1}
    (source/'持续验收状态.json').write_text(json.dumps(status),encoding='utf-8')
    (source/'行情初始哈希.json').write_text('{}',encoding='utf-8')
    event_id = store.rows('events')[0]['event_id']
    (source/'本机接收审计.jsonl').write_text(json.dumps({'event_id':event_id,'duplicate':False})+'\n',encoding='utf-8')
    spec = importlib.util.spec_from_file_location('checkpoint_audit',ROOT/'scripts'/'长观察复核.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check(source,tmp_path/'原始通过回执.json')['verified_decisions']==1
    directory = tmp_path/'隔离事件副本'
    (directory/'运行').mkdir(parents=True)
    for name in ('持续验收状态.json','行情初始哈希.json','本机接收审计.jsonl'):
        shutil.copyfile(source/name,directory/name)
    destination = directory/'运行'/'监控事件.sqlite3'
    with sqlite3.connect((source/'运行'/'监控事件.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as live:
        with sqlite3.connect(destination) as copied:
            live.backup(copied)
    with sqlite3.connect(destination) as copied:
        if fault=='foreign_key':
            copied.execute('DELETE FROM events WHERE rowid=(SELECT min(rowid) FROM events)')
        elif fault=='snapshot_version':
            copied.execute("UPDATE decisions SET payload=json_set(payload,'$.code_hash',?) "
                'WHERE rowid=(SELECT min(rowid) FROM decisions)',('0'*64,))
        elif fault=='delivered_receipt':
            changed = copied.execute("UPDATE outbox SET delivered_at=NULL WHERE notification_id="
                "(SELECT notification_id FROM outbox WHERE status='DELIVERED' LIMIT 1)")
            assert changed.rowcount==1
        else:
            with (directory/'本机接收审计.jsonl').open('a',encoding='utf-8') as stream:
                stream.write('{"event_id":')
    output = tmp_path/'备份检查回执.json'
    if fault=='incomplete_audit_tail':
        receipt = module.check(directory,output)
        assert receipt['receiver_incomplete_tail_ignored'] is True
        assert receipt['receiver_requests']==1 and receipt['receiver_duplicates']==0
    else:
        with pytest.raises((RuntimeError,ValueError)):
            module.check(directory,output)
        assert not output.exists()
