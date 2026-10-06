"""Controlled kernel interleaving: denial while termination awaits signal."""
import ctypes
from ctypes import wintypes
import subprocess

import pytest

from inv_trend.monitoring.Windows_原子进程 import OwnedWindowsProcess


class PendingTerminationKernel:
    def __init__(self):
        self.signaled = False
        self.waits = []

    def WaitForSingleObject(self, handle, milliseconds):
        self.waits.append(milliseconds)
        if milliseconds:
            self.signaled = True
        return 0 if self.signaled else 258

    def TerminateProcess(self, handle, code):
        ctypes.set_last_error(5)
        return 0

    def GetExitCodeProcess(self, handle, value):
        ctypes.cast(value, ctypes.POINTER(wintypes.DWORD)).contents.value = 23
        return 1


def test_access_denied_pending_exit_requires_positive_handle_evidence():
    kernel = PendingTerminationKernel()
    process = OwnedWindowsProcess(kernel, 101, 202, ('controlled-kernel-contract',))
    process.terminate()
    assert process.returncode == 23
    assert kernel.signaled and 5000 in kernel.waits


@pytest.mark.parametrize('error_code', [5, 6, 87])
def test_live_process_denial_is_not_silently_accepted(error_code):
    class LiveKernel(PendingTerminationKernel):
        def WaitForSingleObject(self, handle, milliseconds):
            self.waits.append(milliseconds)
            return 258
        def TerminateProcess(self, handle, code):
            ctypes.set_last_error(error_code)
            return 0
    kernel = LiveKernel()
    process = OwnedWindowsProcess(kernel, 101, 202, ('controlled-live-contract',))
    with pytest.raises(OSError) as caught:
        process.terminate()
    assert caught.value.winerror == error_code and process.returncode is None
    if error_code == 5:
        assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
        assert 5000 in kernel.waits
    else:
        assert not any(kernel.waits)


def test_known_exit_never_requests_another_termination():
    class ExitedKernel(PendingTerminationKernel):
        def TerminateProcess(self, handle, code):
            pytest.fail('known exited process was terminated again')
    kernel = ExitedKernel()
    kernel.signaled = True
    process = OwnedWindowsProcess(kernel, 101, 202, ('controlled-exit-contract',))
    process.terminate()
    assert process.returncode == 23
