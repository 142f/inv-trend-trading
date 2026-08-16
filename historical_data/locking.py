"""Small dependency-free inter-process file lock for publication boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4


@dataclass
class FileLock:
    path: Path
    timeout: float = 15.0
    poll_interval: float = 0.05
    stale_after: float = 300.0

    _acquired: bool = False
    _token: str | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        token = uuid4().hex
        payload = json.dumps(
            {"pid": os.getpid(), "created_at": time.time(), "token": token},
            ensure_ascii=False,
        ).encode("utf-8")
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                self._remove_if_stale()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring publication lock: {self.path}"
                    )
                time.sleep(self.poll_interval)
                continue
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                raise
            self._token = token
            self._acquired = True
            return

    def _remove_if_stale(self) -> None:
        try:
            before = self.path.stat()
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            after = self.path.stat()
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            owner = None
            try:
                before = after = self.path.stat()
            except FileNotFoundError:
                return
        if not _same_file_state(before, after):
            return
        age = time.time() - after.st_mtime
        owner_state = _owner_process_state(owner)
        if owner_state is True:
            return
        if owner_state is None and age <= self.stale_after:
            return
        try:
            current = self.path.stat()
            if not _same_file_state(after, current):
                return
            self.path.unlink()
        except FileNotFoundError:
            return

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            try:
                owner = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return
            if owner.get("token") != self._token:
                return
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            self._acquired = False
            self._token = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def _owner_process_state(owner: object) -> bool | None:
    """Return True for alive, False for known-dead, and None for unknown."""
    if not isinstance(owner, dict):
        return None
    try:
        pid = int(owner.get("pid", 0))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )
