from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import subprocess
import sys

import pandas as pd
import pytest
import yaml

from inv_trend.cli.监控运行 import main
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.健康 import HealthRecorder
from inv_trend.monitoring.扫描器 import CodeVersion

NOW = pd.Timestamp('2024-01-01T00:00:00Z')


def create(tmp_path):
    store = EventStore(tmp_path/'运行/监控事件.sqlite3')
    ledger = RuntimeLedger(store,tmp_path/'数据')
    ledger.expect('scanner','OLD:D1',30,now=NOW)
    return ledger


def retire(ledger,**kwargs):
    return ledger.retire('scanner','OLD:D1',expected_epoch=kwargs.pop('epoch',1),
                         actor=kwargs.pop('actor','operator'),reason=kwargs.pop('reason','removed target'),
                         now=kwargs.pop('now',NOW+pd.Timedelta(seconds=10)),**kwargs)


def health(ledger):
    return HealthRecorder(ledger,code_version=CodeVersion('a'*40,'b'*64),config_hash='c'*64)


def test_retirement_is_idempotent_and_retains_history(tmp_path):
    ledger=create(tmp_path)
    receipt=retire(ledger)
    assert receipt==retire(ledger,now=NOW+pd.Timedelta(seconds=20))
    assert ledger.expected_components()==[]
    assert ledger.expectations(include_retired=True)[0]['status']=='RETIRED'
    assert len(ledger.expectation_history('scanner','OLD:D1'))==2
    restarted=RuntimeLedger(EventStore(ledger.store.path),ledger.data_root)
    assert restarted.retired_components()=={('scanner','OLD:D1')}
    assert restarted.expectation_history('scanner','OLD:D1')==ledger.expectation_history('scanner','OLD:D1')


def test_different_retirement_request_does_not_mutate(tmp_path):
    ledger=create(tmp_path)
    retire(ledger)
    with pytest.raises(ValueError,match='revision conflict'):
        retire(ledger,reason='different')
    assert len(ledger.expectation_history('scanner','OLD:D1'))==2


def test_new_registration_invalidates_old_retirement_epoch(tmp_path):
    ledger=create(tmp_path)
    other=RuntimeLedger(ledger.store,ledger.data_root)
    other.expect('scanner','OLD:D1',30,now=NOW+pd.Timedelta(seconds=1))
    with pytest.raises(ValueError,match='revision conflict'):
        retire(ledger)
    assert other.expectations()[0]['epoch']==2
    assert other.expectations()[0]['registered_at']==NOW.isoformat()


@pytest.mark.parametrize('unknown',[False,True])
def test_alive_or_unknown_pulse_owner_cannot_be_retired(tmp_path,monkeypatch,unknown):
    ledger=create(tmp_path)
    ledger.pulse('scanner','OLD:D1',now=NOW)
    if unknown:
        monkeypatch.setattr('inv_trend.monitoring.执行记录._owner_process_state',lambda _:None)
    with pytest.raises(ValueError,match='alive or unknown'):
        retire(ledger)
    assert ledger.expected_components()==[('scanner','OLD:D1',30)]


def test_unfinished_scan_prevents_retirement(tmp_path):
    ledger=create(tmp_path)
    ledger.begin('OLD','D1',now=NOW)
    with pytest.raises(ValueError,match='unfinished'):
        retire(ledger)


def test_retired_component_cannot_pulse_without_registration(tmp_path):
    ledger=create(tmp_path)
    retire(ledger)
    with pytest.raises(ValueError,match='explicit registration'):
        ledger.pulse('scanner','OLD:D1',now=NOW+pd.Timedelta(seconds=11))
    assert ledger.heartbeats()==[]


def test_reactivation_requires_new_pulse_and_keeps_old_pulse(tmp_path):
    ledger=create(tmp_path)
    script="""
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.事件存储 import EventStore
import pandas as pd,sys
ledger=RuntimeLedger(EventStore(sys.argv[1]),sys.argv[2])
ledger.pulse('scanner','OLD:D1',now=pd.Timestamp('2024-01-01T00:00Z'))
"""
    process=subprocess.run([sys.executable,'-c',script,str(ledger.store.path),str(ledger.data_root)],
                           capture_output=True,timeout=20)
    assert process.returncode==0,process.stderr
    retire(ledger)
    ledger.expect('scanner','OLD:D1',30,now=NOW+pd.Timedelta(seconds=20))
    assert len(ledger.heartbeats())==1
    result=health(ledger).check_heartbeats(ledger.expected_components(),now=NOW+pd.Timedelta(seconds=21))
    assert result[0]['snapshot']['reasons']==['HEARTBEAT_STARTING']
    ledger.pulse('scanner','OLD:D1',now=NOW+pd.Timedelta(seconds=22))
    result=health(ledger).check_heartbeats(ledger.expected_components(),now=NOW+pd.Timedelta(seconds=23))
    assert result[0]['snapshot']['reasons']==['HEARTBEAT_RECOVERED']
    with pytest.raises(ValueError,match='revision conflict'):
        retire(ledger)


def test_retired_scope_in_old_watchdog_list_is_ignored(tmp_path):
    ledger=create(tmp_path)
    old=ledger.expected_components()
    retire(ledger)
    assert health(ledger).check_heartbeats(old,now=NOW+pd.Timedelta(seconds=40))==[]
    assert ledger.store.rows('events')==[]


def test_audit_insert_failure_rolls_back_lifecycle(tmp_path):
    ledger=create(tmp_path)
    with ledger.store._connection() as db:
        db.execute("CREATE TRIGGER reject_retirement BEFORE INSERT ON runtime_expectation_changes "
                   "WHEN NEW.action='RETIRED' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError,match='audit unavailable'):
        retire(ledger)
    assert ledger.expectations()[0]['epoch']==1
    assert len(ledger.expectation_history('scanner','OLD:D1'))==1


def test_process_exit_between_status_and_audit_rolls_back_then_retries(tmp_path):
    ledger=create(tmp_path)
    with ledger.store._connection() as db:
        db.execute("CREATE TRIGGER crash_retirement BEFORE INSERT ON runtime_expectation_changes "
                   "WHEN NEW.action='RETIRED' BEGIN SELECT exit_process(); END")
    script="""
from contextlib import contextmanager
import os,sys,pandas as pd
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
store=EventStore(sys.argv[1])
ledger=RuntimeLedger(store,sys.argv[2])
original=store._connection
@contextmanager
def connection():
    with original() as db:
        db.create_function('exit_process',0,lambda:os._exit(81))
        yield db
store._connection=connection
ledger.retire('scanner','OLD:D1',expected_epoch=1,actor='operator',reason='removed target',now=pd.Timestamp('2024-01-01T00:00:10Z'))
"""
    process=subprocess.run([sys.executable,'-c',script,str(ledger.store.path),str(ledger.data_root)],
                           capture_output=True,timeout=20)
    assert process.returncode==81,process.stderr
    assert ledger.expectations()[0]['status']=='ACTIVE'
    assert len(ledger.expectation_history('scanner','OLD:D1'))==1
    with ledger.store._connection() as db:
        db.execute('DROP TRIGGER crash_retirement')
    assert retire(ledger)['status']=='RETIRED'


def test_concurrent_different_retirement_requests_only_one_commits(tmp_path):
    ledger=create(tmp_path)
    def attempt(actor):
        try:
            return retire(ledger,actor=actor)['status']
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,['operator-a','operator-b']))
    assert results.count('RETIRED')==1
    assert len(ledger.expectation_history('scanner','OLD:D1'))==2


def test_cli_retirement_and_watchdog_do_not_restore_removed_scope(tmp_path,capsys):
    ledger=create(tmp_path)
    current=datetime.now(timezone.utc)
    ledger.pulse('scanner','BTC:D1',now=current)
    ledger.pulse('scheduler','main',now=current)
    config=tmp_path/'配置.yaml'
    config.write_text(yaml.safe_dump({'data_root':str(ledger.data_root),
        'runtime_dir':str(ledger.store.path.parent),'symbols':['BTC'],'timeframes':['D1'],'refresh':False}),encoding='utf-8')
    assert main(['--config',str(config),'retire-expectation','--component','scanner','--scope','OLD:D1',
                 '--expected-epoch','1','--actor','operator','--reason','removed target'])==0
    capsys.readouterr()
    assert main(['--config',str(config),'watchdog'])==0
    assert 'HEARTBEAT_MISSING' not in capsys.readouterr().out
    assert ledger.retired_components()=={('scanner','OLD:D1')}


def test_cli_rejects_configured_scope_before_retirement(tmp_path):
    ledger=create(tmp_path)
    config=tmp_path/'配置.yaml'
    config.write_text(yaml.safe_dump({'data_root':str(ledger.data_root),
        'runtime_dir':str(ledger.store.path.parent),'symbols':['OLD'],'timeframes':['D1']}),encoding='utf-8')
    assert main(['--config',str(config),'retire-expectation','--component','scanner','--scope','OLD:D1',
                 '--expected-epoch','1','--actor','operator','--reason','removed target'])==1
    assert len(ledger.expectation_history('scanner','OLD:D1'))==1


def test_legacy_expectation_migration_preserves_first_grace(tmp_path):
    store=EventStore(tmp_path/'运行.sqlite3')
    with store._connection() as db:
        db.execute('CREATE TABLE runtime_expectations(component TEXT,scope TEXT,max_age REAL,registered_at TEXT,PRIMARY KEY(component,scope))')
        db.execute('INSERT INTO runtime_expectations VALUES(?,?,?,?)',('scanner','OLD:D1',30,NOW.isoformat()))
    ledger=RuntimeLedger(store,tmp_path/'数据')
    assert ledger.expectations()[0]['reason']=='legacy_registration'
    assert health(ledger).check_heartbeats(ledger.expected_components(),now=NOW+pd.Timedelta(seconds=31))[0]['snapshot']['reasons']==['HEARTBEAT_MISSING']


@pytest.mark.parametrize('table',['runtime_expectation_changes','runtime_expectation_lifecycle'])
def test_missing_persisted_lifecycle_cannot_be_silently_recreated(tmp_path,table):
    ledger=create(tmp_path)
    retire(ledger)
    with ledger.store._connection() as db:
        db.execute('DELETE FROM '+table)
    with pytest.raises(ValueError,match='incomplete or inconsistent'):
        RuntimeLedger(EventStore(ledger.store.path),ledger.data_root)


def test_replacing_history_with_out_of_range_epoch_is_rejected(tmp_path):
    ledger=create(tmp_path)
    retire(ledger)
    with ledger.store._connection() as db:
        db.execute("UPDATE runtime_expectation_changes SET epoch=0 WHERE epoch=1")
    with pytest.raises(ValueError,match='incomplete or inconsistent'):
        RuntimeLedger(EventStore(ledger.store.path),ledger.data_root)


def test_cli_alias_cannot_bypass_configured_target_check(tmp_path):
    ledger=create(tmp_path)
    config=tmp_path/'配置.yaml'
    config.write_text(yaml.safe_dump({'data_root':str(ledger.data_root),
        'runtime_dir':str(ledger.store.path.parent),'symbols':['OLD'],'timeframes':['D1']}),encoding='utf-8')
    assert main(['--config',str(config),'retire-expectation','--component','scanner','--scope','old:1D',
                 '--expected-epoch','1','--actor','operator','--reason','removed target'])==1
    assert len(ledger.expectation_history('scanner','OLD:D1'))==1


def test_retirement_command_never_builds_repository_or_strategy(tmp_path,monkeypatch):
    ledger=create(tmp_path)
    config=tmp_path/'配置.yaml'
    config.write_text(yaml.safe_dump({'data_root':str(ledger.data_root),
        'runtime_dir':str(ledger.store.path.parent),'symbols':['BTC'],'timeframes':['D1']}),encoding='utf-8')
    def unexpected(*args,**kwargs):
        raise AssertionError('retirement must not initialize scanner')
    monkeypatch.setattr('inv_trend.cli.监控运行.build_scanner',unexpected)
    assert main(['--config',str(config),'retire-expectation','--component','scanner','--scope','OLD:D1',
                 '--expected-epoch','1','--actor','operator','--reason','removed target'])==0
