from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

import pandas as pd

from .integrity import frame_hash, json_bytes
from .locking import FileLock
from .models import DataConflictError, DataLineageError, path_text


_EXPECTED_VERSION_UNSET = object()


def require_parquet() -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Parquet support requires: pip install 'pyarrow>=15,<22'"
        ) from exc


class DataLake:
    """Append-only local data lake with recoverable current-version activation."""

    def __init__(self, root: str | Path = "data") -> None:
        self.root = Path(root)
        for folder in (
            "raw",
            "normalized",
            "curated",
            "reference",
            "manifests",
            "dataset_manifests",
            "ingestion_reports",
            "quality_reports",
            "quarantine",
            "logs/ingest",
            "reviews",
            ".locks",
            "operations/current",
        ):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.catalog_path, self._catalog_engine = self._select_catalog_backend()
        self._init_catalog()
        self._recover_activation_journals()

    def _select_catalog_backend(self) -> tuple[Path, str]:
        """Select one catalog deterministically and persist an explicit marker."""
        duckdb_path = self.root / "catalog.duckdb"
        sqlite_path = self.root / "catalog.sqlite3"
        marker_path = self.root / "catalog_backend.json"
        marker: dict[str, Any] | None = None
        if marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DataLineageError(
                    f"invalid catalog backend marker: {marker_path}"
                ) from exc

        if marker is not None:
            engine = str(marker.get("backend") or marker.get("engine") or "")
            filename = str(marker.get("catalog_path") or marker.get("path") or "")
            expected = {"duckdb": duckdb_path.name, "sqlite": sqlite_path.name}
            if engine not in expected or filename != expected[engine]:
                raise DataLineageError(
                    "catalog backend marker contains an unsupported backend/path pair"
                )
            selected = self.root / filename
            existing = [path for path in (duckdb_path, sqlite_path) if path.exists()]
            if any(path != selected for path in existing):
                raise DataLineageError(
                    "catalog backend marker conflicts with an additional catalog file"
                )
            if existing and not selected.exists():
                raise DataLineageError(
                    "catalog backend marker points to a missing catalog"
                )
            self._ensure_catalog_dependency(engine)
            normalized = {
                "backend": engine,
                "catalog_path": filename,
                "schema_version": 1,
            }
            if marker != normalized:
                self._write_mutable_json_atomic(marker_path, normalized)
            return selected, engine

        existing = [path for path in (duckdb_path, sqlite_path) if path.exists()]
        if len(existing) > 1:
            raise DataLineageError(
                "catalog.duckdb and catalog.sqlite3 both exist without an explicit "
                "catalog_backend.json marker"
            )
        if existing:
            selected = existing[0]
            engine = "duckdb" if selected.suffix == ".duckdb" else "sqlite"
        else:
            try:
                import duckdb  # noqa: F401

                selected, engine = duckdb_path, "duckdb"
            except ImportError:
                selected, engine = sqlite_path, "sqlite"
        self._ensure_catalog_dependency(engine)
        self._write_mutable_json_atomic(
            marker_path,
            {"backend": engine, "catalog_path": selected.name, "schema_version": 1},
        )
        return selected, engine

    @staticmethod
    def _ensure_catalog_dependency(engine: str) -> None:
        if engine != "duckdb":
            return
        try:
            import duckdb  # noqa: F401
        except ImportError as exc:
            raise DataLineageError(
                "catalog backend is DuckDB but the duckdb dependency is unavailable"
            ) from exc

    def _connect(self):
        if self._catalog_engine == "duckdb":
            import duckdb

            return duckdb.connect(str(self.catalog_path))
        return sqlite3.connect(self.catalog_path)

    def _init_catalog(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    run_id TEXT PRIMARY KEY, symbol TEXT, instrument_id TEXT,
                    timeframe TEXT, source TEXT, manifest_path TEXT,
                    status TEXT, version TEXT, created_at TEXT,
                    asset_class TEXT DEFAULT '', start_time TEXT DEFAULT '',
                    end_time TEXT DEFAULT '', row_count INTEGER DEFAULT 0,
                    schema_version TEXT DEFAULT '', checksum TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '', raw_source TEXT DEFAULT ''
                )
                """
            )
            self._ensure_catalog_columns(db)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS current_versions (
                    symbol TEXT, timeframe TEXT, version TEXT, run_id TEXT,
                    updated_at TEXT, PRIMARY KEY(symbol, timeframe)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rollbacks (
                    rollback_id TEXT PRIMARY KEY, symbol TEXT, timeframe TEXT,
                    previous_version TEXT, target_version TEXT, actor TEXT,
                    reason TEXT, created_at TEXT
                )
                """
            )

    def _ensure_catalog_columns(self, db) -> None:
        if self._catalog_engine == "duckdb":
            existing = {
                str(row[0])
                for row in db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'datasets' AND table_schema = 'main'"
                ).fetchall()
            }
        else:
            existing = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(datasets)").fetchall()
            }
        columns = {
            "asset_class": "TEXT DEFAULT ''",
            "start_time": "TEXT DEFAULT ''",
            "end_time": "TEXT DEFAULT ''",
            "row_count": "INTEGER DEFAULT 0",
            "schema_version": "TEXT DEFAULT ''",
            "checksum": "TEXT DEFAULT ''",
            "updated_at": "TEXT DEFAULT ''",
            "raw_source": "TEXT DEFAULT ''",
        }
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE datasets ADD COLUMN {name} {definition}")

    def write_frame(
        self,
        frame: pd.DataFrame,
        relative: Path,
        *,
        immutable: bool = True,
    ) -> Path:
        require_parquet()
        path = self.root / relative
        with self.lock(f"artifact:{relative.as_posix()}", timeout=60.0):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if immutable:
                    existing = pd.read_parquet(path)
                    if frame_hash(existing) != frame_hash(frame):
                        raise DataLineageError(
                            "immutable Parquet artifact conflicts with requested "
                            f"content: {path}"
                        )
                    return path
                raise FileExistsError(path)
            # Keep the same-directory atomic-write contract, but do not make
            # the temporary name a second copy of a deeply partitioned
            # Parquet filename.  On Windows a normal pytest/data-lake root
            # plus the old ``.<name>.<pid>.<uuid>.tmp`` suffix can hit the
            # legacy 260-character path boundary before pyarrow opens it.
            # The destination lock already serializes writers for this
            # artifact, so a short UUID is sufficient for a recoverable temp
            # token while preserving atomic ``os.replace`` publication.
            temporary = path.with_name(f".tmp-{uuid4().hex[:16]}")
            try:
                frame.to_parquet(
                    temporary,
                    index=False,
                    engine="pyarrow",
                    compression="zstd",
                )
                with temporary.open("r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                self._fsync_directory(path.parent)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return path

    def write_raw(
        self,
        frame: pd.DataFrame,
        provider: str,
        instrument_id: str,
        run_id: str,
    ) -> Path:
        day = datetime.now(timezone.utc).date().isoformat()
        return self.write_frame(
            frame,
            Path("raw")
            / f"provider={provider}"
            / f"instrument={_safe(instrument_id)}"
            / f"request_date={day}"
            / f"{run_id}.parquet",
        )

    def write_raw_payload(
        self,
        payload: bytes,
        provider: str,
        instrument_id: str,
        request_id: str,
        suffix: str = ".json",
    ) -> Path:
        """Persist the provider's unparsed response before any field mapping."""
        day = datetime.now(timezone.utc).date().isoformat()
        path = (
            self.root
            / "raw"
            / f"provider={provider}"
            / f"instrument={_safe(instrument_id)}"
            / f"request_date={day}"
            / f"{request_id}{suffix}"
        )
        with self.lock(
            f"artifact:{path.relative_to(self.root).as_posix()}", timeout=60.0
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_bytes_immutable(path, payload)
        return path

    def write_normalized(
        self,
        frame: pd.DataFrame,
        asset_class: str,
        instrument_id: str,
        timeframe: str,
    ) -> list[Path]:
        if frame.empty:
            return []
        paths: list[Path] = []
        for year, partition in frame.groupby(frame["timestamp"].dt.year):
            content = pd.util.hash_pandas_object(
                partition.drop(columns=["ingested_at"], errors="ignore"),
                index=False,
            ).values.tobytes()
            key = hashlib.sha256(content).hexdigest()[:20]
            relative = (
                Path("normalized")
                / f"asset_class={asset_class}"
                / f"instrument={_safe(instrument_id)}"
                / f"timeframe={timeframe}"
                / f"year={year}"
                / f"part-{key}.parquet"
            )
            paths.append(self.write_frame(partition, relative))
        return paths

    def publish_curated(
        self,
        frame: pd.DataFrame,
        *,
        symbol: str,
        instrument_id: str,
        asset_class: str,
        timeframe: str,
        version: str,
        run_id: str,
        channel: str = "curated",
        activate: bool = True,
    ) -> Path:
        path = self.write_frame(
            frame,
            Path(channel)
            / f"asset_class={asset_class}"
            / f"instrument={_safe(instrument_id)}"
            / f"timeframe={timeframe}"
            / f"version={version}"
            / "bars.parquet",
        )
        if activate:
            self.activate_curated(
                symbol,
                timeframe,
                version,
                run_id,
                path,
                channel=channel,
            )
        return path

    def activate_curated(
        self,
        symbol: str,
        timeframe: str,
        version: str,
        run_id: str,
        path: Path,
        *,
        channel: str = "curated",
        dataset_manifest_path: Path | None = None,
        expected_current_version: str | None | object = _EXPECTED_VERSION_UNSET,
    ) -> None:
        """Advance current through a lock, journal, verification and rollback."""
        symbol, timeframe = symbol.upper(), timeframe.upper()
        path = Path(path)
        if not path.exists():
            raise DataLineageError(
                f"cannot activate missing curated artifact: {path}"
            )
        if dataset_manifest_path is not None and not Path(
            dataset_manifest_path
        ).exists():
            raise DataLineageError(
                f"cannot activate missing dataset manifest: {dataset_manifest_path}"
            )
        pointer = self._pointer_path(symbol, timeframe, channel)
        payload: dict[str, Any] = {
            "version": version,
            "run_id": run_id,
            "path": path_text(path, self.root),
            "channel": channel,
        }
        if dataset_manifest_path is not None:
            payload["dataset_manifest_path"] = path_text(
                dataset_manifest_path, self.root
            )

        with self.lock(f"current:{channel}:{symbol}:{timeframe}"):
            previous_pointer = self._read_pointer(pointer)
            previous_catalog = (
                self._current_catalog_row(symbol, timeframe)
                if channel == "curated"
                else None
            )
            if previous_pointer == payload:
                if channel != "curated":
                    return
                if previous_catalog and (
                    previous_catalog["version"] == version
                    and previous_catalog["run_id"] == run_id
                ):
                    return
            if expected_current_version is not _EXPECTED_VERSION_UNSET:
                expected = (
                    None
                    if expected_current_version is None
                    else str(expected_current_version)
                )
                actual = (
                    str(previous_pointer.get("version"))
                    if previous_pointer is not None
                    else None
                )
                if actual != expected:
                    raise DataLineageError(
                        f"stale publication for {symbol}/{timeframe}: expected "
                        f"current {expected}, found {actual}"
                    )

            journal_path = self._activation_journal_path(
                symbol, timeframe, channel
            )
            journal = {
                "symbol": symbol,
                "timeframe": timeframe,
                "channel": channel,
                "previous_pointer": previous_pointer,
                "previous_catalog": previous_catalog,
                "target_pointer": payload,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_mutable_json_atomic(journal_path, journal)
            try:
                self._set_pointer(pointer, payload)
                if channel == "curated":
                    self._set_current_catalog(symbol, timeframe, version, run_id)
                    self._assert_pointer_catalog_consistent(
                        symbol, timeframe, version, run_id
                    )
            except Exception as exc:
                try:
                    self._restore_activation_state(
                        pointer,
                        symbol,
                        timeframe,
                        channel,
                        previous_pointer,
                        previous_catalog,
                    )
                except Exception as rollback_exc:
                    raise DataLineageError(
                        "activation failed and rollback could not restore "
                        f"{symbol}/{timeframe}; recovery journal retained at "
                        f"{journal_path}"
                    ) from rollback_exc
                self._unlink_and_sync(journal_path)
                raise DataLineageError(
                    f"activation failed for {symbol}/{timeframe}; previous "
                    "state restored"
                ) from exc
            self._unlink_and_sync(journal_path)

    def _assert_pointer_catalog_consistent(
        self,
        symbol: str,
        timeframe: str,
        version: str,
        run_id: str | None = None,
    ) -> None:
        pointer = self.current_version(symbol, timeframe)
        with self._connect() as db:
            row = db.execute(
                "SELECT version, run_id FROM current_versions "
                "WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
        run_matches = run_id is None or (
            row is not None
            and str(row[1]) == run_id
            and pointer is not None
            and str(pointer.get("run_id")) == run_id
        )
        if (
            pointer is None
            or row is None
            or str(row[0]) != version
            or pointer.get("version") != version
            or not run_matches
        ):
            raise DataLineageError(
                f"pointer/catalog drift for {symbol}/{timeframe}: "
                f"pointer={pointer and pointer.get('version')}, "
                f"catalog={row and row[0]}, expected={version}"
            )

    def _activation_journal_path(
        self, symbol: str, timeframe: str, channel: str
    ) -> Path:
        token = hashlib.sha256(
            f"{channel}:{symbol}:{timeframe}".encode()
        ).hexdigest()[:20]
        return self.root / "operations" / "current" / f"{token}.json"

    def _recover_activation_journals(self) -> None:
        journal_root = self.root / "operations" / "current"
        for journal_path in sorted(journal_root.glob("*.json")):
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                symbol = str(journal["symbol"])
                timeframe = str(journal["timeframe"])
                channel = str(journal["channel"])
                target = dict(journal["target_pointer"])
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise DataLineageError(
                    f"invalid activation recovery journal: {journal_path}"
                ) from exc
            pointer = self._pointer_path(symbol, timeframe, channel)
            with self.lock(f"current:{channel}:{symbol}:{timeframe}"):
                current_pointer = self._read_pointer(pointer)
                current_catalog = (
                    self._current_catalog_row(symbol, timeframe)
                    if channel == "curated"
                    else None
                )
                committed = current_pointer == target and (
                    channel != "curated"
                    or (
                        current_catalog is not None
                        and current_catalog["version"] == str(target["version"])
                        and current_catalog["run_id"] == str(target["run_id"])
                    )
                )
                if not committed:
                    self._restore_activation_state(
                        pointer,
                        symbol,
                        timeframe,
                        channel,
                        journal.get("previous_pointer"),
                        journal.get("previous_catalog"),
                    )
                self._unlink_and_sync(journal_path)

    def _restore_activation_state(
        self,
        pointer: Path,
        symbol: str,
        timeframe: str,
        channel: str,
        previous_pointer: dict[str, Any] | None,
        previous_catalog: dict[str, Any] | None,
    ) -> None:
        if previous_pointer is None:
            self._unlink_and_sync(pointer)
        else:
            self._set_pointer(pointer, previous_pointer)
        if channel != "curated":
            return
        if previous_catalog is None:
            self._delete_current_catalog(symbol, timeframe)
        else:
            self._set_current_catalog(
                symbol,
                timeframe,
                str(previous_catalog["version"]),
                str(previous_catalog["run_id"]),
            )

    def _current_catalog_row(
        self, symbol: str, timeframe: str
    ) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT version, run_id, updated_at FROM current_versions "
                "WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
        if row is None:
            return None
        return {
            "version": str(row[0]),
            "run_id": str(row[1]),
            "updated_at": str(row[2]),
        }

    def repair_current(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """Resync the catalog current row from the filesystem pointer."""
        symbol, timeframe = symbol.upper(), timeframe.upper()
        pointer = self._pointer_path(symbol, timeframe)
        with self.lock(f"current:curated:{symbol}:{timeframe}"):
            payload = self._read_pointer(pointer)
            if payload is None:
                self._delete_current_catalog(symbol, timeframe)
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "action": "cleared",
                }
            self._set_current_catalog(
                symbol,
                timeframe,
                str(payload["version"]),
                str(payload["run_id"]),
            )
            self._assert_pointer_catalog_consistent(
                symbol,
                timeframe,
                str(payload["version"]),
                str(payload["run_id"]),
            )
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "action": "repaired",
                "version": payload["version"],
            }

    def _pointer_path(
        self, symbol: str, timeframe: str, channel: str = "curated"
    ) -> Path:
        name = "current.json" if channel == "curated" else f"{channel}_current.json"
        return (
            self.root
            / "curated"
            / f"symbol={symbol}"
            / f"timeframe={timeframe}"
            / name
        )

    def _set_pointer(self, pointer: Path, payload: dict[str, Any]) -> None:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        self._write_mutable_json_atomic(pointer, payload)

    @staticmethod
    def _read_pointer(pointer: Path) -> dict[str, Any] | None:
        if not pointer.exists():
            return None
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataLineageError(f"invalid current pointer: {pointer}") from exc
        if not isinstance(payload, dict):
            raise DataLineageError(f"invalid current pointer payload: {pointer}")
        return payload

    def _set_current_catalog(
        self, symbol: str, timeframe: str, version: str, run_id: str
    ) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM current_versions WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            )
            db.execute(
                "INSERT INTO current_versions VALUES (?, ?, ?, ?, ?)",
                (
                    symbol,
                    timeframe,
                    version,
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def _delete_current_catalog(self, symbol: str, timeframe: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM current_versions WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            )

    def current_version(
        self, symbol: str, timeframe: str
    ) -> dict[str, Any] | None:
        pointer = self._pointer_path(symbol.upper(), timeframe.upper())
        payload = self._read_pointer(pointer)
        if payload is None:
            return None
        target = str(payload.get("path", "")).replace("\\", "/")
        channel = str(payload.get("channel", ""))
        if channel == "legacy" or "/asset_class=legacy/" in target:
            return None
        return payload

    def list_versions(self, symbol: str, timeframe: str) -> list[str]:
        rows = list(
            (self.root / "curated").glob(
                f"asset_class=*/instrument=*/timeframe={timeframe}/"
                "version=*/bars.parquet"
            )
        )
        result = []
        for path in rows:
            sample = pd.read_parquet(path, columns=["symbol"])
            if not sample.empty and str(sample["symbol"].iloc[0]) == symbol:
                result.append(path.parent.name.split("=", 1)[1])
        return sorted(result)

    def curated_version_path(
        self, symbol: str, timeframe: str, version: str
    ) -> Path:
        """Resolve one immutable curated artifact without consulting ``current``.

        A D1 workflow pins the content-addressed dataset version during its
        data-update stage.  Later stages must therefore resolve the recorded
        artifact directly, rather than re-reading a mutable current pointer.
        The symbol check is intentional: a version directory is not accepted
        merely because it has the requested name.
        """

        require_parquet()
        symbol, timeframe = symbol.upper(), timeframe.upper()
        version = _validated_version(version)
        matches: list[Path] = []
        for path in (self.root / "curated").glob(
            f"asset_class=*/instrument=*/timeframe={timeframe}/version=*/bars.parquet"
        ):
            if path.parent.name != f"version={version}":
                continue
            try:
                sample = pd.read_parquet(path, columns=["symbol"])
            except Exception as exc:
                raise DataLineageError(
                    f"cannot inspect curated version for {symbol}/{timeframe}: {path}"
                ) from exc
            if "symbol" in sample and (sample["symbol"].astype(str).str.upper() == symbol).any():
                matches.append(path)
        if len(matches) != 1:
            if not matches:
                raise FileNotFoundError(
                    f"curated version not found for {symbol}/{timeframe}: {version}"
                )
            raise DataLineageError(
                f"multiple curated artifacts match {symbol}/{timeframe}/{version}: {matches}"
            )
        return matches[0]

    def read_bars_version(
        self, symbol: str, timeframe: str, version: str
    ) -> pd.DataFrame:
        """Read exactly one immutable curated dataset version.

        This deliberately does not assert that the version is still current.
        It does, however, fail closed if the selected Parquet payload claims a
        different dataset version, which prevents a malformed artifact from
        being used as a pinned input.
        """

        version = _validated_version(version)
        frame = self._read_bars_from_paths(
            symbol,
            timeframe,
            [self.curated_version_path(symbol, timeframe, version)],
        )
        if "dataset_version" not in frame:
            raise DataLineageError(
                f"curated version {version} for {symbol}/{timeframe} has no dataset_version"
            )
        actual_versions = set(frame["dataset_version"].astype(str).dropna())
        if actual_versions != {version}:
            raise DataLineageError(
                f"curated artifact version mismatch for {symbol}/{timeframe}: "
                f"expected {version}, got {sorted(actual_versions)}"
            )
        return frame

    def rollback(
        self,
        symbol: str,
        timeframe: str,
        version: str,
        reason: str,
        *,
        actor: str | None = None,
    ) -> None:
        symbol, timeframe = symbol.upper(), timeframe.upper()
        matching = []
        for path in (self.root / "curated").glob(
            f"asset_class=*/instrument=*/timeframe={timeframe}/"
            f"version={version}/bars.parquet"
        ):
            sample = pd.read_parquet(path, columns=["symbol"])
            if not sample.empty and str(sample["symbol"].iloc[0]) == symbol:
                matching.append(path)
        if len(matching) != 1:
            raise FileNotFoundError(
                f"curated version not found for {symbol}/{timeframe}: {version}"
            )
        previous = self.current_version(symbol, timeframe)
        dataset_manifest_path = (
            self.root / "dataset_manifests" / f"{version}.json"
        )
        self.activate_curated(
            symbol,
            timeframe,
            version,
            "rollback",
            matching[0],
            dataset_manifest_path=(
                dataset_manifest_path if dataset_manifest_path.exists() else None
            ),
        )
        with self._connect() as db:
            created_at = datetime.now(timezone.utc).isoformat()
            rollback_id = hashlib.sha256(
                f"{symbol}{timeframe}{version}{created_at}".encode()
            ).hexdigest()[:24]
            db.execute(
                "INSERT INTO rollbacks(rollback_id,symbol,timeframe,"
                "previous_version,target_version,actor,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    rollback_id,
                    symbol,
                    timeframe,
                    (previous or {}).get("version"),
                    version,
                    actor or os.getenv("USERNAME", "unknown"),
                    reason,
                    created_at,
                ),
            )

    def read_bars(
        self, symbol: str, timeframe: str, *, curated: bool = True
    ) -> pd.DataFrame:
        require_parquet()
        symbol, timeframe = symbol.upper(), timeframe.upper()
        if curated:
            current = self.current_version(symbol, timeframe)
            if current is None:
                raise FileNotFoundError(
                    f"no curated bars for {symbol}/{timeframe}"
                )
            paths = [self._resolve_root_relative(str(current["path"]))]
        else:
            paths = list(
                (self.root / "normalized").glob(
                    f"asset_class=*/instrument=*/timeframe={timeframe}/"
                    "year=*/*.parquet"
                )
            )
        return self._read_bars_from_paths(symbol, timeframe, paths)

    def _read_bars_from_paths(
        self, symbol: str, timeframe: str, paths: list[Path]
    ) -> pd.DataFrame:
        """Load and normalize one or more already-resolved data artifacts."""

        symbol, timeframe = symbol.upper(), timeframe.upper()
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
        order = (
            ["timestamp", "ingested_at"]
            if "ingested_at" in frame
            else ["timestamp"]
        )
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
            formal = self._pointer_path(symbol, timeframe)
            if formal.exists():
                payload = json.loads(formal.read_text(encoding="utf-8"))
                if "/asset_class=legacy/" in str(payload.get("path", "")).replace(
                    "\\", "/"
                ):
                    pointer = formal
        if not pointer.exists():
            raise FileNotFoundError(f"no legacy bars for {symbol}/{timeframe}")
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        frame = pd.read_parquet(
            self._resolve_root_relative(str(payload["path"]))
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame.sort_values("timestamp").drop_duplicates(
            "timestamp", keep="last"
        )

    def write_json(
        self,
        payload: dict[str, Any],
        relative: Path,
        *,
        idempotent: bool = False,
    ) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json_bytes(payload)
        if path.exists():
            if idempotent and path.read_bytes() == encoded:
                return path
            raise FileExistsError(
                f"immutable JSON artifact already exists: {path}"
            )
        self._write_bytes_atomic(path, encoded)
        return path

    def log_event(self, payload: dict[str, Any]) -> Path:
        day = datetime.now(timezone.utc).date().isoformat()
        path = self.root / "logs" / "ingest" / f"{day}.jsonl"
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            )
        return path

    def lock(self, name: str, *, timeout: float = 15.0) -> FileLock:
        token = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        return FileLock(self.root / ".locks" / f"{token}.lock", timeout=timeout)

    def _write_bytes_immutable(self, path: Path, payload: bytes) -> None:
        if path.exists():
            if path.read_bytes() != payload:
                raise DataLineageError(
                    "immutable byte artifact conflicts with requested content: "
                    f"{path}"
                )
            return
        self._write_bytes_atomic(path, payload)

    def _write_mutable_json_atomic(
        self, path: Path, payload: dict[str, Any]
    ) -> None:
        self._write_bytes_atomic(path, json_bytes(payload), replace=True)

    def _write_bytes_atomic(
        self, path: Path, payload: bytes, *, replace: bool = False
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not replace:
            raise FileExistsError(path)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists() and not replace:
                raise FileExistsError(path)
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _unlink_and_sync(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(path.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def review(
        self,
        run_id: str,
        decision: str,
        reason: str = "",
        *,
        actor: str = "",
        published_version: str = "",
        approval_decision: str = "",
        reviewed_at: str | None = None,
    ) -> Path:
        if decision not in {"approved", "rejected"}:
            raise ValueError("review decision must be approved or rejected")
        payload = {
            "run_id": run_id,
            "decision": decision,
            "reason": reason,
            "actor": actor
            or os.getenv("USERNAME")
            or os.getenv("USER")
            or "unknown",
            "published_version": published_version,
            "approval_decision": approval_decision,
            "reviewed_at": reviewed_at
            or datetime.now(timezone.utc).isoformat(),
        }
        path = self.root / "reviews" / f"{run_id}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DataLineageError(f"invalid review record: {path}") from exc
            semantic_keys = (
                "run_id",
                "decision",
                "reason",
                "actor",
                "published_version",
                "approval_decision",
            )
            if all(
                existing.get(key, "") == payload.get(key, "")
                for key in semantic_keys
            ):
                return path
            raise DataLineageError(
                f"review {run_id} already has a different immutable decision"
            )
        return self.write_json(payload, Path("reviews") / f"{run_id}.json")

    def list_reviews(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        decided: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / "reviews").glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            decided[str(item["run_id"])] = item
        for candidate in sorted(
            (self.root / "reviews").glob("candidate=*/bars.parquet")
        ):
            run_id = candidate.parent.name.split("=", 1)[1]
            rows.append(
                decided.pop(
                    run_id,
                    {
                        "run_id": run_id,
                        "decision": "pending",
                        "candidate_path": str(candidate),
                    },
                )
            )
        rows.extend(decided.values())
        return rows

    def catalog(
        self,
        run_id: str,
        symbol: str,
        timeframe: str,
        source: str,
        manifest: Path,
        *,
        instrument_id: str = "",
        status: str = "NORMALIZED",
        version: str = "",
        asset_class: str = "",
        start_time: str = "",
        end_time: str = "",
        row_count: int = 0,
        schema_version: str = "",
        checksum: str = "",
        raw_source: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO datasets VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    symbol,
                    instrument_id,
                    timeframe,
                    source,
                    str(manifest),
                    status,
                    version,
                    now,
                    asset_class,
                    start_time,
                    end_time,
                    row_count,
                    schema_version,
                    checksum,
                    now,
                    raw_source,
                ),
            )

    def locate_raw(self, instrument_id: str, run_id: str) -> Path:
        """Locate the immutable provider-parsed raw frame for one download run."""
        matches = list(
            (self.root / "raw").glob(
                f"provider=*/instrument={_safe(instrument_id)}/"
                f"request_date=*/{run_id}.parquet"
            )
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise FileNotFoundError(f"no raw frame for {instrument_id}/{run_id}")
        raise DataLineageError(
            f"multiple raw frames match {instrument_id}/{run_id}: {matches}"
        )


def _safe(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_")


def _validated_version(value: str) -> str:
    """Return a portable version token or reject a path-like value."""

    version = str(value).strip()
    if not version or version in {".", ".."} or "/" in version or "\\" in version:
        raise DataLineageError(f"invalid dataset version: {value!r}")
    return version


def _raise_on_conflicting_duplicates(frame: pd.DataFrame) -> None:
    """Fail closed when a curated view still contains conflicting OHLCV bars."""
    if frame.empty or "timestamp" not in frame:
        return
    value_columns = [
        column
        for column in ("open", "high", "low", "close", "volume")
        if column in frame
    ]
    if not value_columns:
        return
    stamps = pd.to_datetime(frame["timestamp"], utc=True).to_numpy()
    duplicated = pd.Series(stamps).duplicated(keep=False).to_numpy()
    if not duplicated.any():
        return
    for timestamp in pd.unique(stamps[duplicated]):
        mask = stamps == timestamp
        rows = frame.loc[mask, value_columns].astype(float)
        if rows.empty:
            continue
        if (rows - rows.iloc[0]).abs().max().max() > 1e-9:
            raise DataConflictError(
                f"conflicting OHLCV bars share timestamp {timestamp}: "
                f"{len(rows)} rows; manual review required"
            )
