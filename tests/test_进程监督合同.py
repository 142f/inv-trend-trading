"""Owned processes must stop even when a native scanner thread cannot return."""
import sys
import time

import pytest

from tests.test_监控扫描与回放合同 import market as market, current_time
from tests.test_数据封版合同 import service as service


CHILD = '''
import ctypes, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import pandas as pd
from inv_trend.data.api import HistoricalDataService
from inv_trend.monitoring.运行配置 import RuntimeConfig
from inv_trend.monitoring.运行时 import MonitoringRuntime
from tests.test_监控扫描与回放合同 import scanner, current_time
repo = HistoricalDataService(sys.argv[1], providers={})
base, start = pd.Timestamp(sys.argv[4]), float(sys.argv[5])
marker = Path(sys.argv[3])
def update(*args, **kwargs):
    if not marker.exists():
        marker.write_text('first-owned-worker-blocked', encoding='utf-8')
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateEventW.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            handle = kernel.CreateEventW(None, True, False, None)
            if not handle: raise ctypes.WinError(ctypes.get_last_error())
            kernel.WaitForSingleObject(handle, 0xffffffff)
        else:
            from threading import Event
            Event().wait()
    raise RuntimeError('update must be disabled on recovered worker')
cfg = RuntimeConfig(data_root=Path(sys.argv[1]), runtime_dir=Path(sys.argv[2]),
    symbols=('BTC',), timeframes=('D1',), refresh=not marker.exists(), paper_equity=100000.,
    interval_seconds=.3, poll_seconds=.05, scan_timeout_seconds=2., heartbeat_timeout_seconds=5.)
monitor = scanner(repo, Path(sys.argv[2]) / '监控事件.sqlite3')
with MonitoringRuntime(monitor, cfg, updater=update,
        clock=lambda: (base + pd.Timedelta(seconds=time.monotonic()-start)).to_pydatetime(warn=False)) as runtime:
    runtime.serve(stop_requested=lambda: Path(sys.argv[-1]).exists())
'''


def test_owned_native_hang_is_stopped_restarted_and_deduplicated(market, tmp_path):
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    repository, _ = market
    child = tmp_path / '隔离监督子进程.py'
    child.write_text(CHILD, encoding='utf-8')
    marker = tmp_path / '首轮阻塞标记'
    import pandas as pd
    base, began = pd.Timestamp(current_time(repository)), time.monotonic()
    cfg = RuntimeConfig(data_root=repository.lake.root, runtime_dir=tmp_path / 'runtime',
        symbols=('BTC',), timeframes=('D1',), refresh=False,
        interval_seconds=.3, poll_seconds=.05, scan_timeout_seconds=2.,
        supervision={'startup_timeout_seconds':8., 'hard_scan_timeout_seconds':3.,
            'restart_delay_seconds':.05,'graceful_stop_seconds':.1,'max_restarts':2,
            'restart_window_seconds':60.})
    with ProcessSupervisor(cfg, command=(sys.executable,str(child),str(repository.lake.root),
        str(cfg.runtime_dir),str(marker),base.isoformat(),str(began)),
        clock=lambda: base + pd.Timedelta(seconds=time.monotonic()-began)) as supervisor:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            supervisor.tick()
            successes = [r for r in supervisor.ledger.tasks() if r['status'] == 'SUCCESS']
            if len(successes) >= 12:
                break
            time.sleep(.05)
        assert len(successes) >= 12
        runs = supervisor.processes()
        assert len(runs) == 2
        assert runs[0]['end_reason'] == 'SCANNER_HARD_TIMEOUT'
        assert any(r['status'] == 'INTERRUPTED' for r in supervisor.ledger.tasks())
        events = supervisor.ledger.store.rows('events')
        assert sum(r['kind'] == 'MARKET_SIGNAL' for r in events) == 1
        assert any(r['code'] == 'SCANNER_HARD_TIMEOUT' for r in events)
        pid = supervisor.child.pid
    assert supervisor.child is None
    assert supervisor.processes()[-1]['end_reason'] == 'SUPERVISOR_STOPPED'
    assert all(r['finished_at'] is not None for r in supervisor.processes())
    assert pid > 0


def test_timeout_observation_does_not_overwrite_newer_worker_pulse(market, tmp_path, monkeypatch):
    from concurrent.futures import Future
    from datetime import timedelta
    from inv_trend.monitoring.运行时 import _JobGuard
    from tests.test_监控运行链合同 import runtime
    with runtime(market,tmp_path) as monitor:
        target = monitor.targets[0]
        guard = _JobGuard(1.,lambda:2.,monitor._stop,started=0.)
        monitor._inflight[target.key] = (target,Future(),guard)
        original = monitor.scanner.record_failure
        later = monitor.clock()+timedelta(seconds=1)
        def worker_finished_first(*args,**kwargs):
            monitor.ledger.pulse('scanner',target.key,now=later,details={'status':'FINISHED'})
            return original(*args,**kwargs)
        monkeypatch.setattr(monitor.scanner,'record_failure',worker_finished_first)
        monitor._progress()
        row = next(r for r in monitor.ledger.heartbeats() if r['component']=='scanner')
        assert row['details']['status'] == 'FINISHED'
        with pytest.raises(ValueError,match='backwards'):
            monitor.ledger.pulse('scanner',target.key,now=monitor.clock())
        monitor._inflight.clear()


def test_restart_budget_is_durable_and_does_not_spawn_forever(tmp_path):
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    cfg = RuntimeConfig(data_root=tmp_path / 'data',runtime_dir=tmp_path / 'runtime',
        supervision={'max_restarts':1,'restart_delay_seconds':.05})
    command = (sys.executable,'-c','raise SystemExit(3)')
    with ProcessSupervisor(cfg,command=command) as supervisor:
        deadline = time.monotonic()+10
        while not supervisor.exhausted and time.monotonic()<deadline:
            supervisor.tick()
            time.sleep(.05)
        assert supervisor.exhausted and len(supervisor.processes()) == 2
    with ProcessSupervisor(cfg,command=command) as restarted:
        assert restarted.tick()['status'] == 'EXHAUSTED'
        assert restarted.child is None and len(restarted.processes()) == 2


def test_alive_record_is_never_adopted_or_killed(tmp_path):
    import os
    import pandas as pd
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    supervisor = ProcessSupervisor(RuntimeConfig(data_root=tmp_path/'data',runtime_dir=tmp_path/'runtime'))
    with supervisor.ledger.store._connection() as db:
        db.execute('INSERT INTO supervisor_processes VALUES(?,?,?,?,?,?,?)',
            ('foreign','old-owner',os.getpid(),os.getpid(),pd.Timestamp.now(tz='UTC').isoformat(),None,None))
    with pytest.raises(RuntimeError,match='refusing PID adoption'):
        with supervisor:
            pytest.fail('alive or unknown stored process cannot be adopted')
    assert supervisor.processes()[0]['finished_at'] is None
    assert supervisor.child is None


def test_same_runtime_supervisors_cannot_overlap(tmp_path):
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    cfg = RuntimeConfig(data_root=tmp_path/'data',runtime_dir=tmp_path/'runtime')
    with ProcessSupervisor(cfg):
        with pytest.raises(TimeoutError):
            with ProcessSupervisor(cfg):
                pytest.fail('second supervisor acquired the same kernel lock')


@pytest.mark.parametrize('settings',[{'max_restarts':True},{'restart_delay_seconds':0},
    {'hard_scan_timeout_seconds':float('nan')},{'unknown':1}])
def test_supervision_settings_fail_closed(settings):
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    with pytest.raises(ValueError):
        RuntimeConfig(supervision=settings)


def test_windows_parent_kernel_exit_closes_only_owned_job(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    from inv_trend.data.locking import _owner_process_state
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    if os.name != 'nt':
        pytest.skip('Windows Job and kernel-exit contract')
    pidfile = tmp_path/'被监督进程PID.txt'
    outer = '''
import ctypes, sys
from pathlib import Path
from inv_trend.monitoring.进程监督 import ProcessSupervisor
from inv_trend.monitoring.运行配置 import RuntimeConfig
cfg = RuntimeConfig(data_root=Path(sys.argv[1])/'data',runtime_dir=Path(sys.argv[1])/'runtime')
with ProcessSupervisor(cfg, command=(sys.executable,'-c','import time; time.sleep(120)')) as service:
    service.tick()
    Path(sys.argv[2]).write_text(str(service.child.pid),encoding='utf-8')
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = (ctypes.c_void_p,ctypes.c_uint)
    kernel.TerminateProcess(kernel.GetCurrentProcess(),74)
raise RuntimeError('kernel exit returned')
'''
    result = subprocess.run([sys.executable,'-c',outer,str(tmp_path),str(pidfile)],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
    assert result.returncode == 74, result.stderr.decode(errors='replace')
    child_pid = int(pidfile.read_text(encoding='utf-8'))
    deadline = time.monotonic()+5
    while _owner_process_state({'pid':child_pid}) is not False and time.monotonic()<deadline:
        time.sleep(.05)
    assert _owner_process_state({'pid':child_pid}) is False
    # The test process was never assigned to the child's job.
    assert _owner_process_state({'pid':os.getpid()}) is True
    cfg = RuntimeConfig(data_root=tmp_path/'data',runtime_dir=tmp_path/'runtime')
    with ProcessSupervisor(cfg) as recovered:
        assert recovered.processes()[0]['end_reason'] == 'ORPHAN_EXITED'
        assert recovered.child is None


def test_supervise_command_keeps_repository_out_of_parent(monkeypatch, tmp_path):
    from inv_trend.cli import 监控运行 as cli
    from inv_trend.monitoring import 进程监督 as module
    seen = []
    class Service:
        def __init__(self, config):
            seen.append(config)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def serve(self):
            return 2
    def forbidden(*args, **kwargs):
        raise AssertionError('supervisor parent must not open market Repository')
    monkeypatch.setattr(module,'ProcessSupervisor',Service)
    monkeypatch.setattr(cli,'HistoricalDataService',forbidden)
    assert cli.main(['--data-root',str(tmp_path/'data'),'--runtime-dir',str(tmp_path/'runtime'),'supervise']) == 2
    assert len(seen) == 1


def test_old_unfinished_job_is_not_hidden_by_bounded_history(tmp_path):
    import os
    import pandas as pd
    from inv_trend.monitoring.事件存储 import EventStore
    from inv_trend.monitoring.执行记录 import RuntimeLedger
    ledger = RuntimeLedger(EventStore(tmp_path/'events.sqlite3'),tmp_path/'data')
    now = pd.Timestamp('2024-01-01T00:00:00Z')
    pending = ledger.begin('BTC','D1',now=now)
    with ledger.store._connection() as db:
        db.executemany('INSERT INTO runtime_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
            [(str(index),ledger.owner_id,os.getpid(),'ETH','D1','SUCCESS',now.isoformat(),
              now.isoformat(),None,'{}') for index in range(1001)])
    assert pending not in {row['job_id'] for row in ledger.tasks(limit=1000)}
    assert [row['job_id'] for row in ledger.active_tasks()] == [pending]
