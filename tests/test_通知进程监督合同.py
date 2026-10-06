"""Notification supervision shares owned process management, never market access."""
import sys
import time
from dataclasses import replace

import pandas as pd
import pytest

from inv_trend.cli import 监控运行 as cli
from inv_trend.monitoring.进程监督 import ProcessSupervisor
from inv_trend.monitoring.运行配置 import RuntimeConfig


CHANNEL = 'webhook:' + 'a'*64


def config(tmp_path):
    return RuntimeConfig(data_root=tmp_path/'data',runtime_dir=tmp_path/'runtime',
        refresh=False,scan_timeout_seconds=1,heartbeat_timeout_seconds=.5,
        supervision={'startup_timeout_seconds':.5,'hard_scan_timeout_seconds':2,
            'graceful_stop_seconds':.05,'restart_delay_seconds':.05,
            'max_restarts':2,'restart_window_seconds':60})


def test_notify_honors_preexisting_owned_stop_marker_without_delivery(tmp_path,monkeypatch,capsys):
    marker = tmp_path/'停止.flag'
    marker.write_text('stop',encoding='utf-8')
    monkeypatch.setenv('TEST_NOTIFICATION_ENDPOINT','http://127.0.0.1:9/test')
    def forbidden(*args,**kwargs):
        pytest.fail('notification must not open market data or deliver after stop')
    monkeypatch.setattr(cli,'build_scanner',forbidden)
    from inv_trend.notifications.队列 import NotificationWorker
    monkeypatch.setattr(NotificationWorker,'run_once',forbidden)
    assert cli.main(['--data-root',str(tmp_path/'data'),'--runtime-dir',str(tmp_path/'runtime'),
        'notify','--webhook-env','TEST_NOTIFICATION_ENDPOINT','--serve','--stop-file',str(marker)]) == 0
    capsys.readouterr()


def test_notification_supervisor_checks_owned_channel_heartbeat(tmp_path):
    cfg = config(tmp_path)
    mono = [0.]
    base = pd.Timestamp.now(tz='UTC')
    with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',notification_channel_id=CHANNEL,
            command=(sys.executable,'-c','import time;time.sleep(120)'),
            monotonic=lambda:mono[0],clock=lambda:base+pd.Timedelta(seconds=mono[0])) as supervisor:
        supervisor.tick()
        mono[0] = .1
        supervisor.ledger.pulse('notification',CHANNEL,now=base+pd.Timedelta(seconds=.1))
        # The parent owns its own pulse, but cannot use it as evidence of child health.
        assert supervisor.tick()['status'] == 'RUNNING'
        assert supervisor.pulse_seen is None
        mono[0] = .2
        with supervisor.ledger.store._connection() as db:
            db.execute('UPDATE runtime_heartbeats SET pid=? WHERE component=? AND scope=?',
                (supervisor.child.pid,'notification',CHANNEL))
        assert supervisor.tick()['status'] == 'RUNNING'
        assert supervisor.pulse_seen is not None
        mono[0] = .8
        result = supervisor.tick()
        assert result['reason'] == 'NOTIFICATION_HEARTBEAT_MISSING'
        assert supervisor.processes()[0]['end_reason'] == result['reason']
        assert any(row['kind']=='NOTIFICATION_HEALTH' for row in supervisor.ledger.store.rows('events'))


def test_runtime_and_notification_supervisors_have_independent_locks_and_history(tmp_path):
    cfg = config(tmp_path)
    command = (sys.executable,'-c','import time;time.sleep(120)')
    with ProcessSupervisor(cfg,command=command) as runtime:
        runtime.tick()
        with ProcessSupervisor(cfg,command=command,webhook_env='TEST_NOTIFICATION_ENDPOINT',
                notification_channel_id=CHANNEL) as notification:
            notification.tick()
            assert len(runtime.processes()) == len(notification.processes()) == 1
            assert runtime.processes()[0]['child_pid'] != notification.processes()[0]['child_pid']
            with pytest.raises(TimeoutError):
                with ProcessSupervisor(cfg,command=command,webhook_env='TEST_NOTIFICATION_ENDPOINT',
                        notification_channel_id=CHANNEL):
                    pytest.fail('same channel supervision cannot overlap')


def seed_notification(store):
    from tests.test_监控状态与事务合同 import decision,facts
    from inv_trend.monitoring.状态机 import transition
    before = store.state('BTC','D1')
    outcome = transition(before,facts(close_confirmed=True))
    snapshot = decision(before,outcome)
    store.commit(snapshot,before,outcome.after)
    return snapshot


def test_default_notification_child_delivers_real_http_and_stops_cooperatively(tmp_path,monkeypatch):
    from tests.test_通知渠道合同 import receiver
    from inv_trend.notifications.渠道 import WebhookNotifier
    cfg = replace(config(tmp_path),poll_seconds=1,heartbeat_timeout_seconds=3,
        supervision={'startup_timeout_seconds':8,'hard_scan_timeout_seconds':2,
            'graceful_stop_seconds':1,'restart_delay_seconds':.05,'max_restarts':2,'restart_window_seconds':60})
    with receiver() as (url,received):
        monkeypatch.setenv('TEST_NOTIFICATION_ENDPOINT',url)
        channel = WebhookNotifier(url)
        with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',
                notification_channel_id=channel.channel_id) as supervisor:
            snapshot = seed_notification(supervisor.ledger.store)
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                supervisor.tick()
                rows = supervisor.ledger.store.rows('outbox')
                if any(r['status']=='DELIVERED' for r in rows): break
                time.sleep(.05)
            assert len(received)==1 and received[0]['body']['snapshot_id']==snapshot.decision_id
            process = supervisor.child
        assert process.returncode==0
        assert supervisor.processes()[0]['end_reason']=='SUPERVISOR_STOPPED'
        for path in cfg.runtime_dir.glob('*.yaml'):
            assert url not in path.read_text(encoding='utf-8')
        assert url not in (cfg.runtime_dir/'监督子进程.log').read_text(encoding='utf-8')


NATIVE_CHILD = '''
import ctypes,os,sys,time
from pathlib import Path
from inv_trend.monitoring.事件存储 import EventStore
from inv_trend.monitoring.执行记录 import RuntimeLedger
from inv_trend.monitoring.健康 import HealthRecorder
from inv_trend.monitoring.扫描器 import CodeVersion
from inv_trend.notifications.渠道 import WebhookNotifier
from inv_trend.notifications.队列 import NotificationWorker
channel = WebhookNotifier(os.environ['TEST_NOTIFICATION_ENDPOINT'],timeout_seconds=.5)
original = channel.send
marker = Path(sys.argv[3])
def send(envelope):
    receipt = original(envelope)
    if not marker.exists():
        marker.write_text('remote accepted before native hang',encoding='utf-8')
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p,ctypes.c_uint)
        handle = kernel.CreateEventW(None,True,False,None)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        kernel.WaitForSingleObject(handle,0xffffffff)
    return receipt
channel.send = send
store = EventStore(Path(sys.argv[1])/'监控事件.sqlite3')
ledger = RuntimeLedger(store,Path(sys.argv[2]))
health = HealthRecorder(ledger,code_version=CodeVersion.capture(),config_hash='native-notify-test')
worker = NotificationWorker(store,channel,health=health,lease_seconds=2,heartbeat_timeout_seconds=1)
while not Path(sys.argv[-1]).exists():
    worker.run_once()
    time.sleep(.05)
'''


def test_native_notification_hang_restarts_and_recovers_expired_lease(tmp_path,monkeypatch):
    from tests.test_通知渠道合同 import receiver
    from inv_trend.notifications.渠道 import WebhookNotifier
    from inv_trend.data.locking import _owner_process_state
    cfg = replace(config(tmp_path),heartbeat_timeout_seconds=1,
        supervision={'startup_timeout_seconds':8,'hard_scan_timeout_seconds':2,
            'graceful_stop_seconds':.1,'restart_delay_seconds':.05,'max_restarts':2,'restart_window_seconds':60})
    child = tmp_path/'隔离通知原生阻塞.py'
    child.write_text(NATIVE_CHILD,encoding='utf-8')
    with receiver() as (url,received):
        monkeypatch.setenv('TEST_NOTIFICATION_ENDPOINT',url)
        channel = WebhookNotifier(url)
        with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',notification_channel_id=channel.channel_id,
                command=(sys.executable,str(child),str(cfg.runtime_dir),str(cfg.data_root),str(tmp_path/'阻塞标记'))) as supervisor:
            seed_notification(supervisor.ledger.store)
            deadline = time.monotonic()+20
            while time.monotonic()<deadline:
                supervisor.tick()
                attempts = supervisor.ledger.store.rows('notification_attempts')
                if any(r['status']=='DELIVERED' for r in attempts): break
                time.sleep(.05)
            assert len(received)==2
            assert len({r['identity'] for r in received})==1
            assert sorted(r['status'] for r in attempts)==['DELIVERED','LEASE_EXPIRED']
            runs = supervisor.processes()
            assert len(runs)==2 and runs[0]['end_reason']=='NOTIFICATION_HEARTBEAT_MISSING'
            assert _owner_process_state({'pid':runs[0]['child_pid']}) is False
            assert supervisor.ledger.tasks()==[]
            assert sum(r['kind']=='MARKET_SIGNAL' for r in supervisor.ledger.store.rows('events'))==1


@pytest.mark.parametrize('arguments',[
    {'webhook_env':'TEST_NOTIFICATION_ENDPOINT'},
    {'notification_channel_id':CHANNEL},
    {'webhook_env':'bad=name','notification_channel_id':CHANNEL},
    {'webhook_env':'TEST_NOTIFICATION_ENDPOINT','notification_channel_id':'http://secret.example/token'},
    {'webhook_env':True,'notification_channel_id':CHANNEL},
])
def test_invalid_notification_supervision_identity_fails_before_storage(tmp_path,arguments):
    cfg = config(tmp_path)
    with pytest.raises(ValueError): ProcessSupervisor(cfg,**arguments)
    assert not cfg.runtime_dir.exists()


def test_cli_notification_supervision_keeps_repository_and_secret_out_of_parent_command(tmp_path,monkeypatch):
    import inv_trend.monitoring.进程监督 as module
    received = []
    monkeypatch.setenv('TEST_NOTIFICATION_ENDPOINT','http://127.0.0.1:9/PRIVATE_TOKEN')
    def forbidden(*args,**kwargs): pytest.fail('notification supervisor must not access market data')
    monkeypatch.setattr(cli,'build_scanner',forbidden)
    monkeypatch.setattr(cli,'HistoricalDataService',forbidden)
    class Supervisor:
        def __init__(self,cfg,**kwargs): received.append((cfg,kwargs))
        def __enter__(self): return self
        def __exit__(self,*args): return None
        def serve(self): return 2
    monkeypatch.setattr(module,'ProcessSupervisor',Supervisor)
    assert cli.main(['--data-root',str(tmp_path/'data'),'--runtime-dir',str(tmp_path/'runtime'),
        'supervise-notify','--webhook-env','TEST_NOTIFICATION_ENDPOINT'])==2
    assert received[0][0].refresh is False
    assert received[0][1]['webhook_env']=='TEST_NOTIFICATION_ENDPOINT'
    assert received[0][1]['notification_channel_id'].startswith('webhook:')
    assert 'PRIVATE_TOKEN' not in repr(received)


def test_notification_restart_budget_is_persistent_and_independent_of_runtime(tmp_path):
    cfg = replace(config(tmp_path),supervision={'startup_timeout_seconds':.5,'hard_scan_timeout_seconds':2,
        'graceful_stop_seconds':.05,'restart_delay_seconds':.05,'max_restarts':0,'restart_window_seconds':60})
    command = (sys.executable,'-c','import time;time.sleep(120)')
    with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',notification_channel_id=CHANNEL,
            command=command) as first:
        first.tick()
    with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',notification_channel_id=CHANNEL,
            command=command) as restarted:
        assert restarted.tick()['status']=='EXHAUSTED'
        assert restarted.child is None
    with ProcessSupervisor(cfg,command=command) as runtime:
        assert runtime.tick()['status']=='STARTING'
        assert len(runtime.processes())==1


def test_stop_marker_between_notification_items_prevents_next_delivery(tmp_path,monkeypatch):
    marker = tmp_path/'批次停止.flag'
    monkeypatch.setenv('TEST_NOTIFICATION_ENDPOINT','http://127.0.0.1:9/test')
    from inv_trend.notifications.队列 import NotificationWorker
    calls = []
    def first_then_stop(self):
        calls.append('one')
        marker.write_text('stop',encoding='utf-8')
        return {'accepted':True,'error':None}
    monkeypatch.setattr(NotificationWorker,'run_once',first_then_stop)
    assert cli.main(['--data-root',str(tmp_path/'data'),'--runtime-dir',str(tmp_path/'runtime'),
        'notify','--webhook-env','TEST_NOTIFICATION_ENDPOINT','--serve','--stop-file',str(marker)])==0
    assert calls==['one']


def test_idle_notification_poll_longer_than_heartbeat_does_not_force_restart(tmp_path,monkeypatch):
    from tests.test_通知渠道合同 import receiver
    from inv_trend.notifications.渠道 import WebhookNotifier
    cfg = replace(config(tmp_path),poll_seconds=10,heartbeat_timeout_seconds=.6,
        supervision={'startup_timeout_seconds':8,'hard_scan_timeout_seconds':2,
            'graceful_stop_seconds':1,'restart_delay_seconds':.05,'max_restarts':2,'restart_window_seconds':60})
    with receiver() as (url,received):
        monkeypatch.setenv('TEST_NOTIFICATION_ENDPOINT',url)
        with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',
                notification_channel_id=WebhookNotifier(url).channel_id) as supervisor:
            pulses = set()
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                supervisor.tick()
                if supervisor.pulse_seen: pulses.add(supervisor.pulse_seen)
                if len(pulses)>=5: break
                time.sleep(.05)
            assert len(pulses)>=5 and len(supervisor.processes())==1
            assert received==[]


def test_scope_insert_failure_rolls_back_start_record_and_stops_child(tmp_path):
    import sqlite3
    from inv_trend.data.locking import _owner_process_state
    cfg = config(tmp_path)
    with ProcessSupervisor(cfg,webhook_env='TEST_NOTIFICATION_ENDPOINT',notification_channel_id=CHANNEL,
            command=(sys.executable,'-c','import time;time.sleep(120)')) as supervisor:
        with supervisor.ledger.store._connection() as db:
            db.execute("CREATE TRIGGER fail_scope BEFORE INSERT ON supervisor_scopes "
                "BEGIN SELECT RAISE(ABORT,'injected scope insert failure'); END")
        created = []
        from inv_trend.monitoring.进程监督 import _WindowsJob
        original_launch = _WindowsJob.launch
        from unittest.mock import patch
        def capture(job,*args,**kwargs):
            process = original_launch(job,*args,**kwargs)
            created.append(process.pid)
            return process
        with patch.object(_WindowsJob,'launch',capture):
            with pytest.raises(sqlite3.IntegrityError,match='scope insert failure'): supervisor.tick()
        assert supervisor.child is None and len(created)==1
        assert _owner_process_state({'pid':created[0]}) is False
        with supervisor.ledger.store._connection() as db:
            assert db.execute('SELECT count(*) FROM supervisor_processes').fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM supervisor_scopes').fetchone()[0]==0
