"""SQLite catalog storage behind the data lake.

Single responsibility: create/migrate the catalog database, resolve which
backend it represents, and read/write the "current version" row.  Business
rules about which frame is authoritative stay in the data lake.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from inv_trend.infra.目录故障 import catalog_access
from .models import CatalogBackendConflictError, DataLineageError


class CatalogStore:
    """Catalog database access mixed into DataLake."""

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
            if not isinstance(marker, dict):
                raise DataLineageError("catalog backend marker must be a JSON object")
            if "schema_version" in marker and (
                type(marker["schema_version"]) is not int or marker["schema_version"] != 1
            ):
                raise DataLineageError("unsupported catalog backend marker schema_version")
            for primary, alias in (("backend", "engine"), ("catalog_path", "path")):
                if primary in marker and alias in marker and marker[primary] != marker[alias]:
                    raise CatalogBackendConflictError(f"conflicting catalog marker {primary}/{alias}")

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
                raise CatalogBackendConflictError(
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
            raise CatalogBackendConflictError(
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

    @contextmanager
    def _connect(self):
        if self._catalog_engine == "duckdb":
            import duckdb
            errors, constraints = (duckdb.Error,), (duckdb.ConstraintException,)
        else:
            errors, constraints = (sqlite3.DatabaseError,), (sqlite3.IntegrityError,)
        with catalog_access(errors, constraints):
            db = (duckdb.connect(str(self.catalog_path), config={'threads': 1})
                  if self._catalog_engine == 'duckdb' else sqlite3.connect(self.catalog_path))
            try:
                db.execute('BEGIN TRANSACTION')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

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

    def _current_catalog_row(
        self, symbol: str, timeframe: str, *, db=None
    ) -> dict[str, str] | None:
        if db is None:
            with self._connect() as connection:
                return self._current_catalog_row(symbol, timeframe, db=connection)
        row = self._current_row(db, symbol, timeframe)
        if row is None:
            return None
        return {
            "version": str(row[0]),
            "run_id": str(row[1]),
            "updated_at": str(row[2]),
        }

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


__all__ = ["CatalogStore"]
