"""Durable local I/O, redaction, SQLite transactions and task leases."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import socket
import sqlite3
import threading
import time
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    secret = re.compile(r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization|private[_-]?key)", re.I)
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if secret.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)((?:password|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def beneath(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"path must stay below workspace: {path}")
    return path


def segment(value):
    value = str(value)
    if not value or value in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith((".", " ")):
        raise ValueError("invalid storage path segment")
    return value


def sync_directory(path):
    # Windows does not expose portable directory fsync through Python. File
    # flush + same-volume rename is tested separately from power-loss durability.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def connect(path):
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    return db


def transaction(path, callback):
    deadline = time.monotonic() + 30
    for attempt in range(4):
        with closing_connection(path) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                result = callback(db)
                db.commit()
                return result
            except sqlite3.OperationalError as exc:
                db.rollback()
                if not any(word in str(exc).lower() for word in ("locked", "busy")) or attempt == 3:
                    raise
                delay = .1 * 2**attempt + random.uniform(0, .1)
                if time.monotonic() + delay >= deadline:
                    raise
                time.sleep(delay)
            except BaseException:
                db.rollback()
                raise


@contextmanager
def closing_connection(path):
    db = connect(path)
    try:
        yield db
    finally:
        db.close()


def process_identity(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return "DEAD"
            return None
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return None
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "DEAD"
        except OSError:
            pass
        return None


def lease_active(path):
    """Unknown or damaged ownership is protective, never deletion permission."""
    try:
        row = json.loads(Path(path).read_text(encoding="utf-8"))
        if row["host"] != socket.gethostname() or not row.get("process_started_at"):
            return True
        identity = process_identity(int(row["pid"]))
        if identity is None:
            return True
        if identity != "DEAD" and identity == row["process_started_at"]:
            return not bool(row.get("released_at"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
        return age < 120
    except (OSError, ValueError, KeyError, TypeError):
        return True


class Lease:
    def __init__(self, directory, run_id):
        self.path = Path(directory) / "lease.json"
        self.row = dict(pid=os.getpid(), run_id=run_id, started_at=now(), heartbeat_at=now(),
                        host=socket.gethostname(), token=uuid4().hex,
                        process_started_at=process_identity(os.getpid()))
        self.stop = threading.Event()

    def beat(self):
        self.row["heartbeat_at"] = now()
        atomic_write(self.path, encoded(self.row))

    def __enter__(self):
        self.beat()
        def pulse():
            while not self.stop.wait(15):
                self.beat()
        self.thread = threading.Thread(target=pulse, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.row["released_at"] = now()
        self.beat()
