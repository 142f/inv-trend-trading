from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd


def require_parquet() -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Parquet support requires: pip install 'pyarrow>=15,<22'") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DataLake:
    def __init__(self, root: str | Path = "data") -> None:
        self.root = Path(root)
        for folder in ("raw", "normalized", "reference", "manifests", "quality_reports", "quarantine"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self._init_catalog()

    def _init_catalog(self) -> None:
        with sqlite3.connect(self.root / "catalog.sqlite3") as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    run_id TEXT PRIMARY KEY, symbol TEXT, timeframe TEXT, source TEXT,
                    manifest_path TEXT, created_at TEXT
                )
            """)

    def write_frame(self, frame: pd.DataFrame, relative: Path, *, immutable: bool = True) -> Path:
        require_parquet()
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if immutable:
                return path
            raise FileExistsError(path)
        frame.to_parquet(path, index=False, engine="pyarrow")
        return path

    def write_raw(self, frame: pd.DataFrame, asset_class: str, symbol: str, run_id: str) -> Path:
        return self.write_frame(frame, Path("raw") / asset_class / symbol / f"{run_id}.parquet")

    def write_normalized(
        self, frame: pd.DataFrame, asset_class: str, symbol: str, timeframe: str
    ) -> list[Path]:
        paths: list[Path] = []
        if frame.empty:
            return paths
        for year, partition in frame.groupby(frame["timestamp"].dt.year):
            # Operational ingestion time must not defeat content idempotency.
            content_frame = partition.drop(columns=["ingested_at"], errors="ignore")
            content = pd.util.hash_pandas_object(content_frame, index=False).values.tobytes()
            key = hashlib.sha256(content).hexdigest()[:20]
            relative = (
                Path("normalized") / timeframe.lower()
                / f"asset_class={asset_class}" / f"symbol={symbol}"
                / f"timeframe={timeframe}" / f"year={year}" / f"part-{key}.parquet"
            )
            paths.append(self.write_frame(partition, relative))
        return paths

    def read_bars(self, symbol: str, timeframe: str) -> pd.DataFrame:
        require_parquet()
        base = self.root / "normalized" / timeframe.lower()
        paths = list(base.glob(f"asset_class=*/symbol={symbol}/timeframe={timeframe}/year=*/*.parquet"))
        if not paths:
            raise FileNotFoundError(f"no normalized bars for {symbol}/{timeframe}")
        frame = pd.concat((pd.read_parquet(path) for path in paths), ignore_index=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame.sort_values(["timestamp", "ingested_at"]).drop_duplicates("timestamp", keep="last")

    def write_json(self, payload: dict[str, Any], relative: Path) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def catalog(self, run_id: str, symbol: str, timeframe: str, source: str, manifest: Path) -> None:
        with sqlite3.connect(self.root / "catalog.sqlite3") as db:
            db.execute(
                "INSERT OR REPLACE INTO datasets VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, symbol, timeframe, source, str(manifest),
                 datetime.now(timezone.utc).isoformat()),
            )
