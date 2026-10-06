"""Windows 10+ process creation with Job membership before the initial thread runs.

Uses PROC_THREAD_ATTRIBUTE_JOB_LIST, not post-creation PID assignment.
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
"""
from __future__ import annotations

import ctypes
import math
import os
import subprocess
import sys


def _kernel():
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    definitions = {
        'GetCurrentProcess': ((),w.HANDLE),
        'DuplicateHandle': ((w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD),w.BOOL),
        'InitializeProcThreadAttributeList': ((ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.c_size_t)),w.BOOL),
        'UpdateProcThreadAttribute': ((ctypes.c_void_p,w.DWORD,ctypes.c_size_t,ctypes.c_void_p,
                                      ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p),w.BOOL),
        'DeleteProcThreadAttributeList': ((ctypes.c_void_p,),None),
        'CreateProcessW': ((w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,w.DWORD,
                           ctypes.c_void_p,w.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p),w.BOOL),
        'CloseHandle': ((w.HANDLE,),w.BOOL),
        'WaitForSingleObject': ((w.HANDLE,w.DWORD),w.DWORD),
        'GetExitCodeProcess': ((w.HANDLE,ctypes.POINTER(w.DWORD)),w.BOOL),
        'TerminateProcess': ((w.HANDLE,w.UINT),w.BOOL),
    }
    for name,(arguments,result) in definitions.items():
        function = getattr(kernel,name)
        function.argtypes,function.restype = arguments,result
    return kernel


class OwnedWindowsProcess:
    """The subset of Popen used by the supervisor, with one owned process handle."""
    def __init__(self,kernel,handle,pid,command):
        self.kernel,self._handle,self.pid,self.args = kernel,handle,pid,command
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = self.kernel.WaitForSingleObject(self._handle,0)
        if result == 258:  # WAIT_TIMEOUT
            return None
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        from ctypes import wintypes as w
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self._handle,ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.returncode = code.value
        return self.returncode

    def wait(self,timeout=None):
        if self.poll() is not None:
            return self.returncode
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError('process wait timeout must be finite and nonnegative')
        milliseconds = 0xffffffff if timeout is None else min(0xfffffffe,math.ceil(timeout*1000))
        result = self.kernel.WaitForSingleObject(self._handle,milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired(self.args,timeout)
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        if self.poll() is None and not self.kernel.TerminateProcess(self._handle,1):
            error = ctypes.WinError(ctypes.get_last_error())
            if self.poll() is None:
                if error.winerror != 5:
                    raise error
                # Another termination may be cancelling pending I/O before
                # the owned process handle signals. Denial alone is not proof
                # of exit; preserve the original error if the bounded wait fails.
                try:
                    self.wait(timeout=5)
                except subprocess.TimeoutExpired as timeout:
                    raise error from timeout

    kill = terminate

    def close(self):
        if self._handle:
            if not self.kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None


def launch_in_job(command, *, job_handle, cwd, log):
    if os.name != 'nt' or not job_handle:
        raise RuntimeError('atomic Windows creation requires a live owned Job')
    from ctypes import wintypes as w
    import msvcrt

    class Startup(ctypes.Structure):
        _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
            ('x',w.DWORD),('y',w.DWORD),('x_size',w.DWORD),('y_size',w.DWORD),
            ('x_chars',w.DWORD),('y_chars',w.DWORD),('fill',w.DWORD),('flags',w.DWORD),
            ('show',w.WORD),('reserved_size',w.WORD),('reserved_bytes',ctypes.POINTER(w.BYTE)),
            ('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]
    class Extended(ctypes.Structure):
        _fields_ = [('startup',Startup),('attributes',ctypes.c_void_p)]
    class ProcessInfo(ctypes.Structure):
        _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]

    kernel = _kernel()
    size = ctypes.c_size_t()
    ctypes.set_last_error(0)
    kernel.InitializeProcThreadAttributeList(None,2,0,ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    storage = ctypes.create_string_buffer(size.value)
    attributes = ctypes.cast(storage,ctypes.c_void_p)
    initialized,duplicates = False,[]
    info = ProcessInfo()
    try:
        if not kernel.InitializeProcThreadAttributeList(attributes,2,0,ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        initialized = True
        jobs = (w.HANDLE*1)(job_handle)
        if not kernel.UpdateProcThreadAttribute(attributes,0,0x0002000d,jobs,ctypes.sizeof(jobs),None,None):
            raise ctypes.WinError(ctypes.get_last_error())
        current = kernel.GetCurrentProcess()
        with open(os.devnull,'rb') as nul:
            for source in (msvcrt.get_osfhandle(nul.fileno()),msvcrt.get_osfhandle(log.fileno())):
                duplicate = w.HANDLE()
                if not kernel.DuplicateHandle(current,source,current,ctypes.byref(duplicate),0,True,2):
                    raise ctypes.WinError(ctypes.get_last_error())
                duplicates.append(duplicate.value)
            # The Job itself and unrelated inheritable handles are excluded.
            handles = (w.HANDLE*len(duplicates))(*duplicates)
            if not kernel.UpdateProcThreadAttribute(attributes,0,0x00020002,handles,ctypes.sizeof(handles),None,None):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = Extended()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin = duplicates[0]
            startup.startup.stdout = startup.startup.stderr = duplicates[1]
            startup.attributes = attributes
            command_line = subprocess.list2cmdline(command)
            buffer = ctypes.create_unicode_buffer(command_line)
            sys.audit('subprocess.Popen',None,command_line,str(cwd),None)
            if not kernel.CreateProcessW(None,buffer,None,None,True,0x08000000|0x00080000,
                    None,str(cwd),ctypes.byref(startup),ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = OwnedWindowsProcess(kernel,info.process,info.pid,command)
            info.process = None  # Transfer this handle to the returned owner.
            return process
    finally:
        if info.thread:
            kernel.CloseHandle(info.thread)
        if info.process:
            kernel.TerminateProcess(info.process,1)
            kernel.WaitForSingleObject(info.process,5000)
            kernel.CloseHandle(info.process)
        if initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        for handle in duplicates:
            kernel.CloseHandle(handle)
