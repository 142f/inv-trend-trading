"""Shared durable writes; callers retain their own serialization and overwrite policy."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from uuid import uuid4


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(
    path: str | Path,
    content: bytes,
    *,
    replace: bool = True,
    directory_sync: Callable[[Path], None] = sync_directory,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and path.exists():
            raise FileExistsError(path)
        os.replace(temporary, path)
        directory_sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
