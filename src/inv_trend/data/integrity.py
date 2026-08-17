"""Deterministic integrity helpers shared by storage and publication flows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


LINEAGE_COLUMNS = frozenset(
    {
        "ingested_at",
        "request_id",
        "raw_snapshot_id",
        "source_run_id",
        "dataset_version",
        "curated_version",
        "raw_file_hash",
        "quality_score",
        "quality_status",
        "quality_flags",
    }
)


def json_bytes(payload: Any) -> bytes:
    """Serialize JSON exactly as immutable artifacts are written on disk."""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize a value canonically for content-addressed identifiers."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def frame_hash(frame: pd.DataFrame, *, exclude_columns: Iterable[str] = ()) -> str:
    """Return a deterministic semantic hash including schema and row order.

    The hash is independent of the Parquet encoder, so immutable retries can
    validate an existing artifact even when encoder metadata differs.
    """
    excluded = set(exclude_columns)
    columns = [column for column in frame.columns if column not in excluded]
    selected = frame.loc[:, columns]
    digest = hashlib.sha256()
    digest.update(
        canonical_json_bytes(
            {
                "columns": [str(column) for column in selected.columns],
                "dtypes": [str(dtype) for dtype in selected.dtypes],
                "rows": len(selected),
            }
        )
    )
    digest.update(pd.util.hash_pandas_object(selected, index=False).values.tobytes())
    return digest.hexdigest()


def strategy_frame_hash(frame: pd.DataFrame) -> str:
    """Preserve the existing content-addressed dataset-version algorithm."""
    columns = [column for column in frame.columns if column not in LINEAGE_COLUMNS]
    payload = pd.util.hash_pandas_object(
        frame.loc[:, columns], index=False
    ).values.tobytes()
    return sha256_bytes(payload)
