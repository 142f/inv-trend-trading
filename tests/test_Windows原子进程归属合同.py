"""Kernel exit immediately after launch must not leave an unowned process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from inv_trend.data.locking import _owner_process_state


@pytest.mark.parametrize('point',['native_create_return','launch_return','start_recorded'])
def test_parent_kernel_exit_at_launch_boundary_keeps_child_owned(tmp_path,point):
    if os.name != 'nt':
        pytest.skip('Windows process attribute contract')
    record = tmp_path/'创建边界记录.json'
    outer = '''
import ctypes,json,sys
from pathlib import Path
from inv_trend.monitoring.进程监督 import ProcessSupervisor,_WindowsJob
from inv_trend.monitoring.运行配置 import RuntimeConfig
def abort(job,child):
    Path(sys.argv[2]).write_text(json.dumps({'pid':child.pid,'owned':job.owns_pid(child.pid)}),encoding='utf-8')
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = (ctypes.c_void_p,ctypes.c_uint)
    kernel.TerminateProcess(kernel.GetCurrentProcess(),74)
    raise RuntimeError('kernel exit returned')
if sys.argv[3] == 'native_create_return':
    from ctypes import wintypes as w
    from types import SimpleNamespace
    from inv_trend.monitoring import Windows_原子进程 as native
    original_kernel = native._kernel
    class Info(ctypes.Structure):
        _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]
    class FaultKernel:
        def __init__(self): self.actual = original_kernel()
        def __getattr__(self,name): return getattr(self.actual,name)
        def CreateProcessW(self,*args):
            result = self.actual.CreateProcessW(*args)
            if result:
                info = ctypes.cast(args[-1],ctypes.POINTER(Info)).contents
                abort(service.job,SimpleNamespace(pid=info.pid))
            return result
    native._kernel = FaultKernel
elif sys.argv[3] == 'launch_return':
    original = _WindowsJob.launch
    def fault(self,*args,**kwargs):
        child = original(self,*args,**kwargs)
        abort(self,child)
    _WindowsJob.launch = fault
else:
    original = ProcessSupervisor._observe
    def fault(self,code,*args,**kwargs):
        if code == 'PROCESS_STARTED': abort(self.job,self.child)
        return original(self,code,*args,**kwargs)
    ProcessSupervisor._observe = fault
cfg = RuntimeConfig(data_root=Path(sys.argv[1])/'data',runtime_dir=Path(sys.argv[1])/'runtime')
with ProcessSupervisor(cfg,command=(sys.executable,'-c','import time;time.sleep(3)')) as service:
    service.tick()
raise RuntimeError('fault point not reached')
'''
    result = subprocess.run([sys.executable,'-c',outer,str(tmp_path),str(record),point],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
    assert result.returncode == 74, result.stderr.decode(errors='replace')
    payload = json.loads(record.read_text(encoding='utf-8'))
    # The deliberately unowned baseline child exits by itself; never kill a
    # process from its recorded PID to make this test appear successful.
    deadline = time.monotonic()+6
    while _owner_process_state({'pid':payload['pid']}) is not False and time.monotonic()<deadline:
        time.sleep(.05)
    assert payload['owned'] is True
    assert _owner_process_state({'pid':payload['pid']}) is False


def test_atomic_job_attribute_failure_never_falls_back_to_unowned_creation(tmp_path,monkeypatch):
    import ctypes
    from inv_trend.monitoring import Windows_原子进程 as native
    from inv_trend.monitoring import 进程监督 as supervisor
    kernel = native._kernel()
    calls = []
    class Unsupported:
        def __getattr__(self,name):
            return getattr(kernel,name)
        def UpdateProcThreadAttribute(self,*args):
            calls.append(args[2])
            ctypes.set_last_error(50)
            return False
        def CreateProcessW(self,*args):
            pytest.fail('unsupported atomic Job cannot create a child')
    monkeypatch.setattr(native,'_kernel',lambda:Unsupported())
    def forbidden(*args,**kwargs):
        pytest.fail('post-creation Popen fallback is forbidden on Windows')
    monkeypatch.setattr(supervisor.subprocess,'Popen',forbidden)
    job = supervisor._WindowsJob()
    try:
        with (tmp_path/'原子失败.log').open('ab') as log:
            with pytest.raises(OSError) as failure:
                job.launch((sys.executable,'-c','raise SystemExit(0)'),cwd=tmp_path,log=log)
        assert failure.value.winerror == 50 and calls == [0x0002000d]
    finally:
        job.close()


def test_unicode_arguments_cwd_and_null_stdin_survive_native_launch(tmp_path):
    from inv_trend.monitoring.进程监督 import _WindowsJob
    directory = tmp_path/'中文 工作目录'
    directory.mkdir()
    path = directory/'子进程输出.log'
    arguments = ['中文 参数','路径结尾\\','嵌入"双引号','$(不会执行)']
    command = (sys.executable,'-c',
        "import json,os,sys;sys.stdout.reconfigure(encoding='utf-8');"
        "print(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'stdin':sys.stdin.read()},ensure_ascii=False))",
        *arguments)
    job,process = _WindowsJob(),None
    try:
        with path.open('ab') as log:
            process = job.launch(command,cwd=directory,log=log)
            assert process.wait(timeout=10) == 0
        record = json.loads(path.read_text(encoding='utf-8'))
        assert record == {'argv':arguments,'cwd':str(directory),'stdin':''}
    finally:
        job.close()
        if process:
            process.close()


def test_repeated_real_creation_failure_closes_temporary_handles(tmp_path):
    import ctypes
    from ctypes import wintypes as w
    from inv_trend.monitoring.Windows_原子进程 import _kernel
    from inv_trend.monitoring.进程监督 import _WindowsJob
    kernel = _kernel()
    kernel.GetProcessHandleCount.argtypes = (w.HANDLE,ctypes.POINTER(w.DWORD))
    kernel.GetProcessHandleCount.restype = w.BOOL
    def count():
        value = w.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(value))
        return value.value
    job = _WindowsJob()
    try:
        with (tmp_path/'创建失败.log').open('ab') as log:
            command = (str(tmp_path/'确定不存在的程序.exe'),)
            # Warm lazy imports before comparing kernel handle counts.
            with pytest.raises(FileNotFoundError):
                job.launch(command,cwd=tmp_path,log=log)
            before = count()
            for _ in range(12):
                with pytest.raises(FileNotFoundError):
                    job.launch(command,cwd=tmp_path,log=log)
            assert count() <= before+1
    finally:
        job.close()


def test_closed_job_cannot_spawn_a_process(tmp_path):
    from inv_trend.monitoring.进程监督 import _WindowsJob
    job = _WindowsJob()
    job.close()
    with (tmp_path/'关闭Job.log').open('ab') as log:
        with pytest.raises(RuntimeError,match='live owned Job'):
            job.launch((sys.executable,'-c','raise SystemExit(0)'),cwd=tmp_path,log=log)


def test_start_record_failure_stops_the_already_owned_process(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from inv_trend.monitoring.进程监督 import ProcessSupervisor
    from inv_trend.monitoring.运行配置 import RuntimeConfig
    cfg = RuntimeConfig(data_root=tmp_path/'data',runtime_dir=tmp_path/'runtime')
    with ProcessSupervisor(cfg,command=(sys.executable,'-c','import time;time.sleep(120)')) as supervisor:
        original = supervisor.ledger.store._connection
        failed = []
        @contextmanager
        def fault_once():
            if supervisor.child is not None and not failed:
                failed.append(supervisor.child.pid)
                raise RuntimeError('injected start record failure')
            with original() as db:
                yield db
        monkeypatch.setattr(supervisor.ledger.store,'_connection',fault_once)
        with pytest.raises(RuntimeError,match='start record failure'):
            supervisor.tick()
        assert supervisor.child is None and len(failed) == 1
        assert supervisor.processes() == []
        assert _owner_process_state({'pid':failed[0]}) is False
