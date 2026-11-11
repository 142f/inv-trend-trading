"""Atomic file primitives used when publishing daily artifacts.

Single responsibility: create, replace, copy and hash files.  Business rules
about which files to publish live in the artifact_publisher module.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from inv_trend.infra.文件操作 import atomic_write
from inv_trend.infra.integrity import sha256_file, stable_hash

def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n",
        encoding="utf-8",
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _atomic_write_text(path: Path, value: str) -> None:
    # Single shared implementation: a fixed scratch name made concurrent
    # publishes read each other half-written file and left .tmp debris behind.
    atomic_write(path, value.encode("utf-8"))


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Contract and descriptive names point at the same immutable bytes.
        # A hard link avoids doubling large embedded chart-series JSON files.
        os.link(source, destination)
    except OSError:
        # Filesystems without hard-link support retain identical behaviour.
        shutil.copyfile(source, destination)


def _remove_file(path: Path) -> None:
    """Remove only a stale derived file; canonical JSON is never deleted."""

    if path.is_file():
        path.unlink()


def _write_csv(path: Path, rows: list[Mapping[str, Any]], fallback_fields: tuple[str, ...]) -> None:
    fields = list(fallback_fields)
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(str(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _sha256(path: Path) -> str:
    return f"sha256:{sha256_file(path)}"


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return stable_hash(payload)

__all__ = [
    "_atomic_write_json",
    "_atomic_write_text",
    "_copy_file",
    "_csv_value",
    "_payload_hash",
    "_remove_file",
    "_sha256",
    "_write_csv",
    "_write_json",
    "_write_text",
]
