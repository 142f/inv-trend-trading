"""SQLite state store for daily signals and daily-run accounting."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from inv_trend_core.signals import SignalEvent


class SQLiteDailySignalRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_events (
                    signal_id TEXT PRIMARY KEY,
                    signal_key TEXT NOT NULL UNIQUE,
                    instrument_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    trigger_price REAL NOT NULL,
                    dataset_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    notified_at TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    updated INTEGER NOT NULL DEFAULT 0,
                    unchanged INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    stale INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    signals_detected INTEGER NOT NULL DEFAULT 0,
                    signals_new INTEGER NOT NULL DEFAULT 0,
                    signals_duplicate INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS daily_run_instruments (
                    run_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, instrument_id),
                    FOREIGN KEY (run_id) REFERENCES daily_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_cursors (
                    instrument_id TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    last_signal_time TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (instrument_id, timeframe, strategy_version)
                );
                CREATE TABLE IF NOT EXISTS signal_outbox (
                    signal_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    FOREIGN KEY (signal_id) REFERENCES signal_events(signal_id),
                    FOREIGN KEY (run_id) REFERENCES daily_runs(run_id)
                );
                """
            )
            self._ensure_column(db, "signal_events", "run_id", "TEXT")
            self._ensure_column(db, "daily_runs", "delivery_errors", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "daily_runs", "recovered_deliveries", "INTEGER NOT NULL DEFAULT 0")
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        names = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in names:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def start_run(self, run_id: str, started_at: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO daily_runs(run_id, started_at, status) VALUES (?, ?, 'RUNNING')",
                (run_id, started_at),
            )

    def insert_signal(self, event: SignalEvent, run_id: str | None = None) -> bool:
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO signal_events(
                    signal_id, signal_key, instrument_id, symbol, timeframe, signal_type,
                    direction, signal_time, trigger_price, dataset_version, created_at, payload_json
                    , run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.signal_id, event.signal_key, event.instrument_id, event.symbol,
                    event.timeframe, event.signal_type, event.direction, event.signal_time,
                    event.trigger_price, event.dataset_version, event.detected_at, payload, run_id,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted and run_id is not None:
                db.execute(
                    """INSERT INTO signal_outbox(signal_id, run_id, created_at)
                    VALUES (?, ?, ?)""",
                    (event.signal_id, run_id, event.detected_at),
                )
            return inserted

    def commit_events_and_cursor(
        self,
        events: list[SignalEvent],
        *,
        run_id: str,
        instrument_id: str,
        timeframe: str,
        strategy_version: str,
        last_signal_time: str,
        enqueue_notifications: bool = True,
    ) -> tuple[list[SignalEvent], int]:
        """Atomically insert unique events, enqueue them, and advance the cursor."""
        inserted: list[SignalEvent] = []
        with self._connect() as db:
            for event in events:
                payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)
                cursor = db.execute(
                    """INSERT OR IGNORE INTO signal_events(
                        signal_id, signal_key, instrument_id, symbol, timeframe,
                        signal_type, direction, signal_time, trigger_price,
                        dataset_version, created_at, payload_json, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.signal_id, event.signal_key, event.instrument_id,
                        event.symbol, event.timeframe, event.signal_type,
                        event.direction, event.signal_time, event.trigger_price,
                        event.dataset_version, event.detected_at,
                        payload, run_id,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted.append(event)
                    if enqueue_notifications:
                        db.execute(
                            "INSERT INTO signal_outbox(signal_id, run_id, created_at) VALUES (?, ?, ?)",
                            (event.signal_id, run_id, event.detected_at),
                        )
            db.execute(
                """INSERT INTO scan_cursors(
                    instrument_id, timeframe, strategy_version, last_signal_time, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, timeframe,strategy_version) DO UPDATE SET
                    last_signal_time=excluded.last_signal_time,
                    updated_at=excluded.updated_at""",
                (instrument_id, timeframe, strategy_version, last_signal_time,
                 datetime.now(timezone.utc).isoformat()),
            )
        return inserted, len(events) - len(inserted)

    def load_cursor(
        self, instrument_id: str, timeframe: str, strategy_version: str
    ) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT last_signal_time FROM scan_cursors
                WHERE instrument_id=? AND timeframe=? AND strategy_version=?""",
                (instrument_id, timeframe, strategy_version),
            ).fetchone()
        return None if row is None else str(row[0])

    def pending_notifications(self, run_id: str | None = None) -> list[SignalEvent]:
        with self._connect() as db:
            sql = """SELECT e.payload_json FROM signal_outbox o
                JOIN signal_events e ON e.signal_id=o.signal_id
                WHERE o.status IN ('PENDING', 'ERROR')"""
            parameters: tuple[str, ...] = ()
            if run_id is not None:
                sql += " AND o.run_id=?"
                parameters = (run_id,)
            rows = db.execute(sql + " ORDER BY o.created_at", parameters).fetchall()
        return [SignalEvent(**json.loads(row["payload_json"])) for row in rows]

    def mark_notified(self, signal_id: str, notified_at: str | None = None) -> None:
        notified_at = notified_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "UPDATE signal_events SET notified_at=? WHERE signal_id=? AND notified_at IS NULL",
                (notified_at, signal_id),
            )
            db.execute(
                """UPDATE signal_outbox SET status='DELIVERED', delivered_at=?,
                    attempts=attempts+1, last_error=NULL WHERE signal_id=?""",
                (notified_at, signal_id),
            )

    def mark_delivery_error(self, signal_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE signal_outbox SET status='ERROR', attempts=attempts+1,
                    last_error=? WHERE signal_id=?""",
                (error, signal_id),
            )

    def record_instrument(self, run_id: str, row: Mapping[str, Any]) -> None:
        status = str(row.get("run_status", "failed")).lower()
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO daily_run_instruments(
                    run_id, instrument_id, symbol, status, payload_json
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id, str(row.get("instrument_id") or row.get("instrument")),
                    str(row["symbol"]), status,
                    json.dumps(dict(row), ensure_ascii=False, allow_nan=False, default=str),
                ),
            )

    def finish_run(self, run_id: str, completed_at: str, summary: Mapping[str, Any]) -> None:
        if summary.get("delivery_errors", 0):
            status = "COMPLETED_WITH_DELIVERY_ERRORS"
        elif any(summary.get(key, 0) for key in ("failed", "blocked", "stale")):
            status = "FAILED"
        else:
            status = "COMPLETED"
        with self._connect() as db:
            db.execute(
                """UPDATE daily_runs SET completed_at=?, status=?, updated=?, unchanged=?,
                    blocked=?, stale=?, failed=?, signals_detected=?, signals_new=?,
                    signals_duplicate=?, error_count=?, delivery_errors=?,
                    recovered_deliveries=? WHERE run_id=?""",
                (
                    completed_at, status, summary.get("updated", 0), summary.get("unchanged", 0),
                    summary.get("blocked", 0), summary.get("stale", 0), summary.get("failed", 0),
                    summary.get("signals_detected", 0), summary.get("signals_new", 0),
                    summary.get("signals_duplicate", 0), summary.get("error_count", 0),
                    summary.get("delivery_errors", 0), summary.get("recovered_deliveries", 0), run_id,
                ),
            )

    def interrupt_run(self, run_id: str, completed_at: str) -> None:
        """Close a RUNNING record when the operator presses Ctrl+C."""
        with self._connect() as db:
            db.execute(
                """UPDATE daily_runs SET completed_at=?, status='INTERRUPTED',
                    error_count=error_count+1 WHERE run_id=? AND status='RUNNING'""",
                (completed_at, run_id),
            )

    def signal_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0])


__all__ = ["SQLiteDailySignalRepository"]
