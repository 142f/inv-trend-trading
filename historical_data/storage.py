from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd

from .models import DataConflictError, DataLineageError, path_text


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
    """Append-only local data lake with immutable curated versions.

    SQLite remains a dependency-free catalog fallback.  When DuckDB is installed,
    callers can query the Parquet lake directly; catalog semantics stay portable.
    """

    def __init__(self, root: str | Path = "data") -> None:
        self.root = Path(root)
        for folder in (
            "raw", "normalized", "curated", "reference", "manifests",
            "dataset_manifests", "ingestion_reports", "quality_reports",
            "quarantine", "logs/ingest", "reviews",
        ):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        duckdb_path, sqlite_path = self.root / "catalog.duckdb", self.root / "catalog.sqlite3"
        marker_path = self.root / "catalog_backend.json"
        if duckdb_path.exists() and sqlite_path.exists():
            raise DataLineageError("catalog.duckdb and catalog.sqlite3 both exist; explicit catalog migration is required")
        # Existing data roots keep their original backend even if a new optional
        # dependency becomes installed later; migration must be explicit.
        if duckdb_path.exists():
            self.catalog_path, self._catalog_engine = duckdb_path, "duckdb"
        elif sqlite_path.exists():
            self.catalog_path, self._catalog_engine = sqlite_path, "sqlite"
        else:
            try:
                import duckdb  # noqa: F401
                self.catalog_path, self._catalog_engine = duckdb_path, "duckdb"
            except ImportError:
                self.catalog_path, self._catalog_engine = sqlite_path, "sqlite"
        if marker_path.exists():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("engine") != self._catalog_engine or marker.get("path") != self.catalog_path.name:
                raise DataLineageError("catalog backend marker does not match the available catalog")
        else:
            marker_path.write_text(json.dumps({"engine": self._catalog_engine, "path": self.catalog_path.name}, indent=2), encoding="utf-8")
        self._init_catalog()

    def _connect(self):
        if self._catalog_engine == "duckdb":
            import duckdb
            return duckdb.connect(str(self.catalog_path))
        return sqlite3.connect(self.catalog_path)

    def _init_catalog(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    run_id TEXT PRIMARY KEY, symbol TEXT, instrument_id TEXT,
                    timeframe TEXT, source TEXT, manifest_path TEXT,
                    status TEXT, version TEXT, created_at TEXT,
                    asset_class TEXT DEFAULT '', start_time TEXT DEFAULT '',
                    end_time TEXT DEFAULT '', row_count INTEGER DEFAULT 0,
                    schema_version TEXT DEFAULT '', checksum TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '', raw_source TEXT DEFAULT ''
                )
            """)
            self._ensure_catalog_columns(db)
            db.execute("""
                CREATE TABLE IF NOT EXISTS current_versions (
                    symbol TEXT, timeframe TEXT, version TEXT, run_id TEXT,
                    updated_at TEXT, PRIMARY KEY(symbol, timeframe)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS rollbacks (
                    rollback_id TEXT PRIMARY KEY, symbol TEXT, timeframe TEXT,
                    previous_version TEXT, target_version TEXT, actor TEXT, reason TEXT, created_at TEXT
                )
            """)

    def _ensure_catalog_columns(self, db) -> None:
        if self._catalog_engine == "duckdb":
            existing = {
                str(row[0]) for row in db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'datasets' AND table_schema = 'main'"
                ).fetchall()
            }
        else:
            existing = {str(row[1]) for row in db.execute("PRAGMA table_info(datasets)").fetchall()}
        columns = {
            "asset_class": "TEXT DEFAULT ''", "start_time": "TEXT DEFAULT ''",
            "end_time": "TEXT DEFAULT ''", "row_count": "INTEGER DEFAULT 0",
            "schema_version": "TEXT DEFAULT ''", "checksum": "TEXT DEFAULT ''",
            "updated_at": "TEXT DEFAULT ''", "raw_source": "TEXT DEFAULT ''",
        }
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE datasets ADD COLUMN {name} {definition}")

    def write_frame(self, frame: pd.DataFrame, relative: Path, *, immutable: bool = True) -> Path:
        require_parquet()
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if immutable:
                return path
            raise FileExistsError(path)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            frame.to_parquet(temporary, index=False, engine="pyarrow", compression="zstd")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path

    def write_raw(self, frame: pd.DataFrame, provider: str, instrument_id: str, run_id: str) -> Path:
        day = datetime.now(timezone.utc).date().isoformat()
        return self.write_frame(
            frame,
            Path("raw") / f"provider={provider}" / f"instrument={_safe(instrument_id)}"
            / f"request_date={day}" / f"{run_id}.parquet",
        )

    def write_raw_payload(self, payload: bytes, provider: str, instrument_id: str, request_id: str,
                          suffix: str = ".json") -> Path:
        """Persist the provider's unparsed response before any field mapping."""
        day = datetime.now(timezone.utc).date().isoformat()
        path = self.root / "raw" / f"provider={provider}" / f"instrument={_safe(instrument_id)}" / f"request_date={day}" / f"{request_id}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        return path

    def write_normalized(
        self, frame: pd.DataFrame, asset_class: str, instrument_id: str, timeframe: str
    ) -> list[Path]:
        if frame.empty:
            return []
        paths: list[Path] = []
        for year, partition in frame.groupby(frame["timestamp"].dt.year):
            content = pd.util.hash_pandas_object(
                partition.drop(columns=["ingested_at"], errors="ignore"), index=False
            ).values.tobytes()
            key = hashlib.sha256(content).hexdigest()[:20]
            relative = (
                Path("normalized") / f"asset_class={asset_class}"
                / f"instrument={_safe(instrument_id)}" / f"timeframe={timeframe}"
                / f"year={year}" / f"part-{key}.parquet"
            )
            paths.append(self.write_frame(partition, relative))
        return paths

    def publish_curated(
        self, frame: pd.DataFrame, *, symbol: str, instrument_id: str, asset_class: str,
        timeframe: str, version: str, run_id: str, channel: str = "curated", activate: bool = True,
    ) -> Path:
        path = self.write_frame(
            frame,
            Path(channel) / f"asset_class={asset_class}" / f"instrument={_safe(instrument_id)}"
            / f"timeframe={timeframe}" / f"version={version}" / "bars.parquet",
        )
        if activate:
            self.activate_curated(symbol, timeframe, version, run_id, path, channel=channel)
        return path

    def activate_curated(self, symbol: str, timeframe: str, version: str, run_id: str, path: Path,
                         *, channel: str = "curated", dataset_manifest_path: Path | None = None) -> None:
        """Atomically advance the current pointer and catalog for one symbol.

        The pointer is written first, then the catalog; a verification pass
        restores the previous pointer when the two disagree so a partial
        failure never leaves pointer and catalog out of sync.
        """
        previous = self.current_version(symbol, timeframe) if channel == "curated" else None
        pointer = self._pointer_path(symbol, timeframe, channel)
        payload = {"version": version, "run_id": run_id,
                   "path": path_text(path, self.root), "channel": channel}
        if dataset_manifest_path is not None:
            payload["dataset_manifest_path"] = path_text(dataset_manifest_path, self.root)
        self._set_pointer(pointer, payload)
        if channel == "curated":
            self._set_current_catalog(symbol, timeframe, version, run_id)
            try:
                self._assert_pointer_catalog_consistent(symbol, timeframe, version)
            except DataLineageError:
                if previous is not None:
                    self._set_pointer(pointer, previous)
                self._set_current_catalog(symbol, timeframe, str(previous.get("version")), str(previous.get("run_id"))) if previous else None
                raise

    def _assert_pointer_catalog_consistent(self, symbol: str, timeframe: str, version: str) -> None:
        pointer = self.current_version(symbol, timeframe)
        with self._connect() as db:
            row = db.execute("SELECT version FROM current_versions WHERE symbol=? AND timeframe=?",
                             (symbol, timeframe)).fetchone()
        if pointer is None or row is None or str(row[0]) != version or pointer.get("version") != version:
            raise DataLineageError(
                f"pointer/catalog drift for {symbol}/{timeframe}: pointer={pointer and pointer.get('version')}, "
                f"catalog={row and row[0]}, expected={version}"
            )

    def repair_current(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """Resync the catalog current row from the filesystem pointer."""
        pointer = self._pointer_path(symbol, timeframe)
        if not pointer.exists():
            with self._connect() as db:
                db.execute("DELETE FROM current_versions WHERE symbol=? AND timeframe=?", (symbol, timeframe))
            return {"symbol": symbol, "timeframe": timeframe, "action": "cleared"}
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        self._set_current_catalog(symbol, timeframe, str(payload["version"]), str(payload["run_id"]))
        return {"symbol": symbol, "timeframe": timeframe, "action": "repaired", "version": payload["version"]}

    def _pointer_path(self, symbol: str, timeframe: str, channel: str = "curated") -> Path:
        name = "current.json" if channel == "curated" else f"{channel}_current.json"
        return self.root / "curated" / f"symbol={symbol}" / f"timeframe={timeframe}" / name

    def _set_pointer(self, pointer: Path, payload: dict[str, Any]) -> None:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        temporary = pointer.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pointer)

    def _set_current_catalog(self, symbol: str, timeframe: str, version: str, run_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM current_versions WHERE symbol=? AND timeframe=?", (symbol, timeframe))
            db.execute("INSERT INTO current_versions VALUES (?, ?, ?, ?, ?)",
                       (symbol, timeframe, version, run_id, datetime.now(timezone.utc).isoformat()))

    def current_version(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        pointer = self._pointer_path(symbol, timeframe)
        if not pointer.exists():
            return None
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        target = str(payload.get("path", "")).replace("\\", "/")
        channel = str(payload.get("channel", ""))
        # Stale migrations once wrote formal current pointers that point into the
        # legacy channel.  Legacy data must never act as the formal current dataset.
        if channel == "legacy" or "/asset_class=legacy/" in target:
            return None
        return payload

    def list_versions(self, symbol: str, timeframe: str) -> list[str]:
        rows = list((self.root / "curated").glob(f"asset_class=*/instrument=*/timeframe={timeframe}/version=*/bars.parquet"))
        result = []
        for path in rows:
            sample = pd.read_parquet(path, columns=["symbol"])
            if not sample.empty and str(sample["symbol"].iloc[0]) == symbol:
                result.append(path.parent.name.split("=", 1)[1])
        return sorted(result)

    def rollback(self, symbol: str, timeframe: str, version: str, reason: str, *, actor: str | None = None) -> None:
        matching = []
        for path in (self.root / "curated").glob(f"asset_class=*/instrument=*/timeframe={timeframe}/version={version}/bars.parquet"):
            sample = pd.read_parquet(path, columns=["symbol"])
            if not sample.empty and str(sample["symbol"].iloc[0]) == symbol:
                matching.append(path)
        if len(matching) != 1:
            raise FileNotFoundError(f"curated version not found for {symbol}/{timeframe}: {version}")
        previous = self.current_version(symbol, timeframe)
        pointer = self._pointer_path(symbol, timeframe)
        self._set_pointer(pointer, {"version": version, "run_id": "rollback",
                                    "path": path_text(matching[0], self.root), "channel": "curated"})
        self._set_current_catalog(symbol, timeframe, version, "rollback")
        self._assert_pointer_catalog_consistent(symbol, timeframe, version)
        with self._connect() as db:
            db.execute("INSERT INTO rollbacks(rollback_id,symbol,timeframe,previous_version,target_version,actor,reason,created_at) VALUES (?,?,?,?,?,?,?,?)", (
                hashlib.sha256(f"{symbol}{timeframe}{version}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:24],
                symbol, timeframe, (previous or {}).get("version"), version, actor or os.getenv("USERNAME", "unknown"), reason,
                datetime.now(timezone.utc).isoformat(),
            ))

    def read_bars(self, symbol: str, timeframe: str, *, curated: bool = True) -> pd.DataFrame:
        require_parquet()
        if curated:
            current = self.current_version(symbol, timeframe)
            if current is None:
                raise FileNotFoundError(f"no curated bars for {symbol}/{timeframe}")
            paths = [self._resolve_root_relative(str(current["path"]))]
        else:
            paths = list((self.root / "normalized").glob(f"asset_class=*/instrument=*/timeframe={timeframe}/year=*/*.parquet"))
        frames = []
        for path in paths:
            if path.exists():
                frame = pd.read_parquet(path)
                if "symbol" in frame and (frame["symbol"] == symbol).any():
                    frames.append(frame.loc[frame["symbol"] == symbol])
        if not frames:
            raise FileNotFoundError(f"no bars for {symbol}/{timeframe}")
        frame = pd.concat(frames, ignore_index=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        _raise_on_conflicting_duplicates(frame)
        order = ["timestamp", "ingested_at"] if "ingested_at" in frame else ["timestamp"]
        return frame.sort_values(order).drop_duplicates("timestamp", keep="last")

    def _resolve_root_relative(self, raw: str) -> Path:
        """Resolve a stored path against the data root; accepts legacy forms."""
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        root_relative = self.root / candidate
        if root_relative.exists():
            return root_relative
        if candidate.exists():
            return candidate
        return root_relative

    def read_legacy_bars(self, symbol: str, timeframe: str) -> pd.DataFrame:
        require_parquet()
        pointer = self._pointer_path(symbol, timeframe, "legacy")
        if not pointer.exists():
            # Pre-channel-refactor migrations wrote formal current pointers whose
            # target lives in the legacy channel; accept them only for legacy reads.
            formal = self._pointer_path(symbol, timeframe)
            if formal.exists():
                payload = json.loads(formal.read_text(encoding="utf-8"))
                if "/asset_class=legacy/" in str(payload.get("path", "")).replace("\\", "/"):
                    pointer = formal
        if not pointer.exists():
            raise FileNotFoundError(f"no legacy bars for {symbol}/{timeframe}")
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        frame = pd.read_parquet(self._resolve_root_relative(str(payload["path"])))
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")

    def write_json(self, payload: dict[str, Any], relative: Path) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"immutable JSON artifact already exists: {path}")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def log_event(self, payload: dict[str, Any]) -> Path:
        day = datetime.now(timezone.utc).date().isoformat()
        path = self.root / "logs" / "ingest" / f"{day}.jsonl"
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return path

    def review(self, run_id: str, decision: str, reason: str = "", *, actor: str = "",
               published_version: str = "") -> Path:
        if decision not in {"approved", "rejected"}:
            raise ValueError("review decision must be approved or rejected")
        return self.write_json({"run_id": run_id, "decision": decision, "reason": reason,
                                "actor": actor or os.getenv("USERNAME", "unknown"),
                                "published_version": published_version,
                                "reviewed_at": datetime.now(timezone.utc).isoformat()},
                               Path("reviews") / f"{run_id}.json")

    def list_reviews(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        decided: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / "reviews").glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            decided[str(item["run_id"])] = item
        for candidate in sorted((self.root / "reviews").glob("candidate=*/bars.parquet")):
            run_id = candidate.parent.name.split("=", 1)[1]
            rows.append(decided.pop(run_id, {"run_id": run_id, "decision": "pending", "candidate_path": str(candidate)}))
        rows.extend(decided.values())
        return rows

    def catalog(self, run_id: str, symbol: str, timeframe: str, source: str, manifest: Path,
                *, instrument_id: str = "", status: str = "NORMALIZED", version: str = "",
                asset_class: str = "", start_time: str = "", end_time: str = "", row_count: int = 0,
                schema_version: str = "", checksum: str = "", raw_source: str = "") -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                run_id, symbol, instrument_id, timeframe, source, str(manifest), status, version,
                datetime.now(timezone.utc).isoformat(), asset_class, start_time, end_time,
                row_count, schema_version, checksum, datetime.now(timezone.utc).isoformat(), raw_source,
            ))

    def locate_raw(self, instrument_id: str, run_id: str) -> Path:
        """Locate the immutable provider-parsed raw frame for one download run."""
        matches = list(
            (self.root / "raw").glob(f"provider=*/instrument={_safe(instrument_id)}/request_date=*/{run_id}.parquet")
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise FileNotFoundError(f"no raw frame for {instrument_id}/{run_id}")
        raise DataLineageError(f"multiple raw frames match {instrument_id}/{run_id}: {matches}")


def _safe(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_")


def _raise_on_conflicting_duplicates(frame: pd.DataFrame) -> None:
    """Fail closed when a curated view still contains conflicting OHLCV bars."""
    if frame.empty or "timestamp" not in frame:
        return
    value_columns = [c for c in ("open", "high", "low", "close", "volume") if c in frame]
    if not value_columns:
        return
    stamps = pd.to_datetime(frame["timestamp"], utc=True).to_numpy()
    duplicated = pd.Series(stamps).duplicated(keep=False).to_numpy()
    if not duplicated.any():
        return
    unique_stamps = pd.unique(stamps[duplicated])
    for timestamp in unique_stamps:
        mask = stamps == timestamp
        rows = frame.loc[mask, value_columns].astype(float)
        if rows.empty:
            continue
        if (rows - rows.iloc[0]).abs().max().max() > 1e-9:
            raise DataConflictError(
                f"conflicting OHLCV bars share timestamp {timestamp}: {len(rows)} rows; manual review required"
            )
