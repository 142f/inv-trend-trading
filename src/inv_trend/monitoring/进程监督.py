"""Bounded restart of owned runtime processes; never adopt a persisted PID."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
import os
import re
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4

import pandas as pd
import yaml

from inv_trend.data.locking import FileLock, _owner_process_state
from .事件存储 import EventStore
from .执行记录 import RuntimeLedger
from .健康 import HealthRecorder
from .扫描器 import CodeVersion


@dataclass(frozen=True)
class SupervisionPolicy:
    startup_timeout_seconds: float = 120.
    hard_scan_timeout_seconds: float = 180.
    restart_delay_seconds: float = 5.
    graceful_stop_seconds: float = 5.
    max_restarts: int = 5
    restart_window_seconds: float = 300.

    def __post_init__(self):
        for name, value in vars(self).items():
            if name == 'max_restarts':
                if type(value) is not int or value < 0:
                    raise ValueError('supervisor restart budget must be nonnegative')
            elif isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('supervisor timing must be finite and positive')


class _WindowsJob:
    """Closing our job handle stops only descendants assigned to that job."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),
                ('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),
                ('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read','write','other')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),
                ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p,w.LPCWSTR)
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = (w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD)
        kernel.SetInformationJobObject.restype = w.BOOL
        kernel.CloseHandle.argtypes = (w.HANDLE,)
        kernel.CloseHandle.restype = w.BOOL
        kernel.OpenProcess.argtypes = (w.DWORD,w.BOOL,w.DWORD)
        kernel.OpenProcess.restype = w.HANDLE
        kernel.IsProcessInJob.argtypes = (w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL))
        kernel.IsProcessInJob.restype = w.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None,None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def launch(self, command, *, cwd, log):
        if os.name == 'nt':
            from .Windows_原子进程 import launch_in_job
            return launch_in_job(command,job_handle=self.handle,cwd=cwd,log=log)
        return subprocess.Popen(command,cwd=cwd,stdin=subprocess.DEVNULL,stdout=log,
            stderr=log,shell=False,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def owns_pid(self, pid):
        if not self.handle or type(pid) is not int or pid <= 0:
            return False
        from ctypes import wintypes as w
        handle = self.kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return False
        try:
            belongs = w.BOOL()
            return bool(self.kernel.IsProcessInJob(handle,self.handle,ctypes.byref(belongs)) and belongs.value)
        finally:
            self.kernel.CloseHandle(handle)


class ProcessSupervisor:
    def __init__(self, config, *, command=None, clock=None, monotonic=None,
                 webhook_env=None, notification_channel_id=None):
        self.config = config
        if (webhook_env is None) != (notification_channel_id is None):
            raise ValueError('notification supervision requires environment name and channel identity')
        if webhook_env is not None and (not isinstance(webhook_env,str)
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',webhook_env)
                or not isinstance(notification_channel_id,str)
                or not re.fullmatch(r'webhook:[0-9a-f]{64}',notification_channel_id)):
            raise ValueError('notification supervision identity is invalid')
        self.webhook_env,self.notification_channel_id = webhook_env,notification_channel_id
        self.scope = 'runtime' if webhook_env is None else 'notification:' + notification_channel_id
        self.policy = SupervisionPolicy(**config.supervision)
        if self.policy.hard_scan_timeout_seconds <= config.scan_timeout_seconds:
            raise ValueError('hard scan deadline must exceed the cooperative scan timeout')
        if command is not None and (isinstance(command,(str,bytes)) or not command
                or any(not isinstance(v,str) or not v or '\0' in v for v in command)):
            raise ValueError('child command must be a nonempty argument list')
        self.command = tuple(command) if command is not None else None
        self.clock = clock or (lambda: pd.Timestamp.now(tz='UTC'))
        self.monotonic = monotonic or time.monotonic
        config.runtime_dir.mkdir(parents=True,exist_ok=True)
        self.ledger = RuntimeLedger(EventStore(config.runtime_dir / '监控事件.sqlite3'),config.data_root)
        self.health = HealthRecorder(self.ledger,code_version=CodeVersion.capture(),config_hash=config.config_hash)
        with self.ledger.store._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS supervisor_processes('
                'run_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,parent_pid INTEGER NOT NULL,'
                'child_pid INTEGER NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,end_reason TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS supervisor_scopes('
                'run_id TEXT PRIMARY KEY REFERENCES supervisor_processes(run_id),scope TEXT NOT NULL)')
        lock_name = '进程监督.lock' if webhook_env is None else '通知进程监督_' + notification_channel_id[8:] + '.lock'
        self.lock = FileLock(config.runtime_dir / lock_name,timeout=0)
        self.child, self.job, self.log = None, None, None
        self.entered, self.exhausted = False, False
        self.next_start = 0.
        self.seen_jobs = {}

    def processes(self):
        with self.ledger.store._connection() as db:
            return [dict(r) for r in db.execute('SELECT p.* FROM supervisor_processes p '
                'LEFT JOIN supervisor_scopes s ON s.run_id=p.run_id '
                "WHERE COALESCE(s.scope,'runtime')=? ORDER BY p.rowid",(self.scope,))]

    def __enter__(self):
        self.lock.acquire()
        try:
            for row in self.processes():
                if row['finished_at'] is None:
                    if _owner_process_state({'pid':row['child_pid']}) is not False:
                        raise RuntimeError('unfinished supervisor child is alive or unknown; refusing PID adoption')
                    self._journal_finish(row['run_id'],'ORPHAN_EXITED')
            self.entered = True
            return self
        except BaseException:
            self.lock.release()
            raise

    def _journal_finish(self, run_id, reason):
        with self.ledger.store._connection() as db:
            db.execute('UPDATE supervisor_processes SET finished_at=?,end_reason=? '
                'WHERE run_id=? AND finished_at IS NULL',(pd.Timestamp(self.clock()).isoformat(),reason,run_id))

    def _observe(self, code, severity='INFO', **details):
        from .模型 import EventKind
        notification = self.notification_channel_id is not None
        return self.health.observe('supervisor',self.scope if notification else 'main',code,
            now=self.clock(),severity=severity,details=details,stable_details=True,
            kind=EventKind.NOTIFICATION_HEALTH if notification else EventKind.SYSTEM_HEALTH)

    def _start(self):
        now = pd.Timestamp(self.clock())
        recent = [row for row in self.processes() if (now-pd.Timestamp(row['started_at'])).total_seconds()
                  < self.policy.restart_window_seconds]
        if len(recent) >= self.policy.max_restarts + 1:
            self.exhausted = True
            self._observe('RESTART_BUDGET_EXHAUSTED','CRITICAL',starts_in_window=len(recent))
            return
        self.run_id = uuid4().hex
        self.stop_path = self.config.runtime_dir / ('监督停止_' + self.run_id + '.flag')
        config_path = self.config.runtime_dir / ('监督配置_' + self.run_id + '.yaml')
        config_path.write_text(yaml.safe_dump(self.config.to_dict(),allow_unicode=True),encoding='utf-8')
        entry = (sys.executable,'-m','inv_trend.cli.监控运行','--config',str(config_path))
        command = self.command or (entry + ('notify','--webhook-env',self.webhook_env,'--serve')
            if self.webhook_env is not None else entry + ('serve',))
        self.job = _WindowsJob()
        self.log = (self.config.runtime_dir / '监督子进程.log').open('ab')
        try:
            self.child = self.job.launch((*command,'--stop-file',str(self.stop_path)),
                cwd=Path(__file__).resolve().parents[3],log=self.log)
            with self.ledger.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO supervisor_processes VALUES(?,?,?,?,?,?,?)',
                    (self.run_id,self.ledger.owner_id,os.getpid(),self.child.pid,now.isoformat(),None,None))
                db.execute('INSERT INTO supervisor_scopes VALUES(?,?)',(self.run_id,self.scope))
                db.commit()
            self.started = self.monotonic()
            self.last_pulse, self.pulse_seen = self.started, None
            self.started_wall = now
            self.seen_jobs = {}
            self._observe('PROCESS_STARTED',pid=self.child.pid,run_id=self.run_id)
        except BaseException:
            self._stop_child('START_FAILED',graceful=False)
            raise

    def _stop_child(self, reason, *, graceful=True):
        process = self.child
        try:
            if process is not None and process.poll() is None:
                if graceful:
                    self.stop_path.write_text(reason,encoding='utf-8')
                    try:
                        process.wait(timeout=self.policy.graceful_stop_seconds)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process is not None:
                if self.job is not None:
                    self.job.close()
                self._journal_finish(self.run_id,reason)
                self.ledger.recover(now=self.clock())
        finally:
            try:
                if self.job is not None:
                    self.job.close()
            finally:
                try:
                    if process is not None and hasattr(process,'close'):
                        process.close()
                finally:
                    if self.log is not None:
                        self.log.close()
                    self.child, self.job, self.log = None, None, None

    def tick(self):
        if not self.entered:
            raise RuntimeError('supervisor must hold its instance lock')
        if self.exhausted:
            return {'status':'EXHAUSTED'}
        if self.child is None:
            if self.monotonic() >= self.next_start:
                self._start()
            return {'status':'EXHAUSTED' if self.exhausted else 'STARTING'}
        if self.child.poll() is not None:
            reason = 'PROCESS_EXITED'
        else:
            now, mono = pd.Timestamp(self.clock()), self.monotonic()
            component = 'notification' if self.notification_channel_id else 'scheduler'
            scope = self.notification_channel_id or 'main'
            pulse = next((r for r in self.ledger.heartbeats() if r['component']==component
                and r['scope']==scope and self._owns_pid(r['pid'])),None)
            if pulse and self.started_wall <= pd.Timestamp(pulse['observed_at']) <= now:
                identity = (pulse['owner_id'],pulse['observed_at'])
                if identity != self.pulse_seen:
                    self.last_pulse, self.pulse_seen = mono, identity
                    self._observe('PROCESS_READY',pid=self.child.pid)
            reason = None
            if self.pulse_seen is None and mono-self.started > self.policy.startup_timeout_seconds:
                reason = 'PROCESS_STARTUP_TIMEOUT'
            elif self.pulse_seen is not None and mono-self.last_pulse > self.config.heartbeat_timeout_seconds:
                reason = 'NOTIFICATION_HEARTBEAT_MISSING' if self.notification_channel_id else 'SCHEDULER_HEARTBEAT_MISSING'
            for row in (() if self.notification_channel_id else self.ledger.active_tasks()):
                if not self._owns_pid(row['pid']) or row['finished_at'] is not None:
                    continue
                first, age = self.seen_jobs.setdefault(row['job_id'],
                    (mono,max(0.,(now-pd.Timestamp(row['started_at'])).total_seconds())))
                if age+mono-first > self.policy.hard_scan_timeout_seconds:
                    reason = 'SCANNER_HARD_TIMEOUT'
                    break
        if reason:
            try:
                self._observe(reason,'CRITICAL',pid=self.child.pid)
            finally:
                self._stop_child(reason)
            self.next_start = self.monotonic()+self.policy.restart_delay_seconds
            return {'status':'RESTART_WAIT','reason':reason}
        return {'status':'RUNNING','pid':self.child.pid}

    def _owns_pid(self, pid):
        return self.child is not None and (pid == self.child.pid or self.job.owns_pid(pid))

    def serve(self):
        stop = Event()
        while not self.exhausted:
            self.tick()
            stop.wait(min(self.config.poll_seconds,1.))
        return 2

    def __exit__(self,*args):
        try:
            self._stop_child('SUPERVISOR_STOPPED')
        finally:
            self.entered = False
            self.lock.release()
