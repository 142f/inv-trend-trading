"""Shared durable writes; callers retain their own serialization and overwrite policy."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Callable
from uuid import uuid4


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_copy(
    source: str | Path,
    destination: str | Path,
    *,
    replace: bool = True,
    directory_sync: Callable[[Path], None] = sync_directory,
) -> None:
    """Publish an existing file under a new name without a partial reader window.

    The temporary name carries the pid and a uuid so two publishers never share
    one scratch file, and the scratch file is removed on every failure path.
    """

    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not replace and destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        shutil.copyfile(source, temporary)
        if not replace and destination.exists():
            raise FileExistsError(destination)
        os.replace(temporary, destination)
        directory_sync(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


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
