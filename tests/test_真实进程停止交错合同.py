"""Real owned Job/handle and real error 5, with one delayed poll observation."""
import ctypes
import sys

from inv_trend.monitoring.进程监督 import _WindowsJob


def test_real_owned_exit_after_denial_requires_confirmed_handle(tmp_path):
    job = _WindowsJob()
    process = None
    with (tmp_path / '真实停止日志.txt').open('wb') as log:
        try:
            process = job.launch((sys.executable, '-c', 'import time; time.sleep(30)'), cwd=tmp_path, log=log)
            assert job.owns_pid(process.pid) and process.poll() is None
            actual = process.kernel
            class ExitInterleaving:
                delayed_poll = False
                denial_error = None
                def __getattr__(self, name):
                    return getattr(actual, name)
                def WaitForSingleObject(self, handle, milliseconds):
                    if milliseconds == 0 and self.delayed_poll:
                        self.delayed_poll = False
                        return 258  # Controlled delayed observation, not forged exit evidence.
                    return actual.WaitForSingleObject(handle, milliseconds)
                def TerminateProcess(self, handle, code):
                    assert actual.TerminateProcess(handle, code)
                    assert actual.WaitForSingleObject(handle, 5000) == 0
                    result = actual.TerminateProcess(handle, code)
                    self.denial_error = ctypes.get_last_error()
                    assert result == 0 and self.denial_error == 5
                    self.delayed_poll = True
                    return result
            kernel = ExitInterleaving()
            process.kernel = kernel
            process.terminate()
            assert kernel.denial_error == 5 and process.returncode == 1
            assert actual.WaitForSingleObject(process._handle, 0) == 0
        finally:
            job.close()
            if process is not None:
                process.close()
