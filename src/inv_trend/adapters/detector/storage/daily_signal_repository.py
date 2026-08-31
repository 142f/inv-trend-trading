"""SQLite state store for daily signals and daily-run accounting."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Collection, Iterator, Mapping

from inv_trend.core.signals import SignalEvent


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
        except BaseException:
            # A run-level commit may insert events, cursor rows, audit rows,
            # and outbox entries before a later instrument fails.  Never let
            # the context manager's close path turn that partial work into a
            # durable transaction.
            connection.rollback()
            raise
        else:
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
                CREATE TABLE IF NOT EXISTS strategy_session_anchors (
                    instrument_id TEXT NOT NULL,
                    base_timeframe TEXT NOT NULL,
                    anchor_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (instrument_id, base_timeframe)
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
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
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

    def ensure_run(self, run_id: str, started_at: str) -> bool:
        """Create a daily-run row once, allowing a crashed commit to resume.

        ``start_run`` intentionally retains its strict legacy semantics.  The
        staged workflow uses this separate operation because a process may die
        after opening the row but before writing its filesystem receipt.
        """

        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO daily_runs(run_id, started_at, status) VALUES (?, ?, 'RUNNING')",
                (run_id, started_at),
            )
        return cursor.rowcount == 1

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
        notification_event_ids: Collection[str] | None = None,
        notification_signal_ids: Collection[str] | None = None,
    ) -> tuple[list[SignalEvent], int]:
        """Atomically insert unique events, enqueue them, and advance the cursor.

        New callers must select formal outbox rows with
        ``notification_event_ids``.  ``notification_signal_ids`` remains a
        parameter-name compatibility alias with the same execution-only rule.
        """
        inserted: list[SignalEvent] = []
        notification_ids = _resolve_notification_ids(
            events,
            enqueue_notifications=enqueue_notifications,
            notification_event_ids=notification_event_ids,
            notification_signal_ids=notification_signal_ids,
        )
        _validate_execution_notification_events(events, notification_ids)
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
                    if event.signal_id in notification_ids:
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

    def commit_daily_run(
        self,
        *,
        run_id: str,
        started_at: str,
        finished_at: str,
        commits: list[Mapping[str, Any]],
        records: list[dict[str, Any]],
    ) -> tuple[list[SignalEvent], dict[str, int]]:
        """Commit a staged multi-instrument run in one SQLite transaction.

        ``commit_events_and_cursor`` remains the public per-instrument API.
        The D1 workflow uses this stronger operation so an exception while
        preparing a later symbol rolls back every earlier cursor advance,
        outbox insertion, session anchor, and run-instrument payload too.
        No schema change is required: all work uses the existing run, cursor,
        signal, outbox, and audit tables.
        """

        inserted: list[SignalEvent] = []
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO daily_runs(run_id, started_at, status) VALUES (?, ?, 'RUNNING')",
                (run_id, started_at),
            )
            for commit in commits:
                row = commit.get("row")
                if not isinstance(row, dict):
                    raise ValueError("daily run commit requires a mutable instrument row")
                instrument_id = str(commit.get("instrument_id") or "")
                timeframe = str(commit.get("timeframe") or "")
                strategy_version = str(commit.get("strategy_version") or "")
                if "expected_cursor" not in commit:
                    raise ValueError("daily run commit is missing its expected cursor")
                raw_expected_cursor = commit.get("expected_cursor")
                if raw_expected_cursor is not None and not isinstance(raw_expected_cursor, str):
                    raise ValueError("daily run commit has an invalid expected cursor")
                expected_cursor = raw_expected_cursor
                last_signal_time = str(commit.get("last_signal_time") or "")
                session_anchor = str(commit.get("session_anchor") or "")
                if not all((instrument_id, timeframe, strategy_version, last_signal_time, session_anchor)):
                    raise ValueError("daily run commit has incomplete cursor or anchor identity")

                now = datetime.now(timezone.utc).isoformat()
                cursor_row = db.execute(
                    """SELECT last_signal_time FROM scan_cursors
                    WHERE instrument_id=? AND timeframe=? AND strategy_version=?""",
                    (instrument_id, timeframe, strategy_version),
                ).fetchone()
                actual_cursor = None if cursor_row is None else str(cursor_row[0])
                if actual_cursor != expected_cursor:
                    raise ValueError(
                        "D1 cursor changed after strategy-screen; rerun strategy-screen"
                    )
                db.execute(
                    """INSERT OR IGNORE INTO strategy_session_anchors(
                        instrument_id, base_timeframe, anchor_time, created_at
                    ) VALUES (?, ?, ?, ?)""",
                    (instrument_id, timeframe, session_anchor, now),
                )
                anchor = db.execute(
                    """SELECT anchor_time FROM strategy_session_anchors
                    WHERE instrument_id=? AND base_timeframe=?""",
                    (instrument_id, timeframe),
                ).fetchone()
                if anchor is None or str(anchor[0]) != session_anchor:
                    raise ValueError(
                        "D1 session anchor changed after strategy-screen; rerun strategy-screen"
                    )

                events = list(commit.get("events") or ())
                if not all(isinstance(event, SignalEvent) for event in events):
                    raise TypeError("daily run commit events must be SignalEvent instances")
                has_execution_ids = "notification_event_ids" in commit
                notification_ids = _resolve_notification_ids(
                    events,
                    enqueue_notifications=bool(
                        commit.get("enqueue_notifications", True)
                    ),
                    notification_event_ids=(
                        commit.get("notification_event_ids")
                        if has_execution_ids
                        else None
                    ),
                    notification_signal_ids=(
                        None
                        if has_execution_ids
                        else commit.get("notification_signal_ids")
                    ),
                )
                enqueue_notifications = bool(commit.get("enqueue_notifications", True))
                _validate_execution_notification_events(events, notification_ids)
                inserted_for_symbol: list[SignalEvent] = []
                for event in events:
                    payload = json.dumps(
                        event.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False
                    )
                    cursor = db.execute(
                        """INSERT OR IGNORE INTO signal_events(
                            signal_id, signal_key, instrument_id, symbol, timeframe,
                            signal_type, direction, signal_time, trigger_price,
                            dataset_version, created_at, payload_json, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event.signal_id,
                            event.signal_key,
                            event.instrument_id,
                            event.symbol,
                            event.timeframe,
                            event.signal_type,
                            event.direction,
                            event.signal_time,
                            event.trigger_price,
                            event.dataset_version,
                            event.detected_at,
                            payload,
                            run_id,
                        ),
                    )
                    if cursor.rowcount == 1:
                        inserted_for_symbol.append(event)
                        if enqueue_notifications and event.signal_id in notification_ids:
                            db.execute(
                                """INSERT INTO signal_outbox(signal_id, run_id, created_at)
                                VALUES (?, ?, ?)""",
                                (event.signal_id, run_id, event.detected_at),
                            )
                db.execute(
                    """INSERT INTO scan_cursors(
                        instrument_id, timeframe, strategy_version, last_signal_time, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id, timeframe,strategy_version) DO UPDATE SET
                        last_signal_time=excluded.last_signal_time,
                        updated_at=excluded.updated_at""",
                    (instrument_id, timeframe, strategy_version, last_signal_time, now),
                )
                row["signals_new"] = len(inserted_for_symbol)
                row["signals_duplicate"] = len(events) - len(inserted_for_symbol)
                inserted.extend(inserted_for_symbol)

            summary = _daily_run_summary(records, len(inserted))
            for row in records:
                status = str(row.get("run_status", "failed")).lower()
                db.execute(
                    """INSERT OR REPLACE INTO daily_run_instruments(
                        run_id, instrument_id, symbol, status, payload_json
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        str(row.get("instrument_id") or row.get("instrument")),
                        str(row["symbol"]),
                        status,
                        json.dumps(dict(row), ensure_ascii=False, allow_nan=False, default=str),
                    ),
                )
            db.execute(
                """UPDATE daily_runs SET completed_at=?, status=?, updated=?, unchanged=?,
                    blocked=?, stale=?, failed=?, signals_detected=?, signals_new=?,
                    signals_duplicate=?, error_count=?, delivery_errors=?,
                    recovered_deliveries=? WHERE run_id=?""",
                (
                    finished_at,
                    _terminal_run_status(summary),
                    summary["updated"],
                    summary["unchanged"],
                    summary["blocked"],
                    summary["stale"],
                    summary["failed"],
                    summary["signals_detected"],
                    summary["signals_new"],
                    summary["signals_duplicate"],
                    summary["error_count"],
                    summary["delivery_errors"],
                    summary["recovered_deliveries"],
                    run_id,
                ),
            )
        return inserted, summary

    def get_or_create_session_anchor(
        self,
        instrument_id: str,
        base_timeframe: str,
        first_complete_time: str,
    ) -> str:
        """Return a stable D1 grouping anchor for multi-session bars."""

        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO strategy_session_anchors(
                    instrument_id, base_timeframe, anchor_time, created_at
                ) VALUES (?, ?, ?, ?)""",
                (
                    instrument_id,
                    base_timeframe,
                    first_complete_time,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            row = db.execute(
                """SELECT anchor_time FROM strategy_session_anchors
                WHERE instrument_id=? AND base_timeframe=?""",
                (instrument_id, base_timeframe),
            ).fetchone()
        if row is None:  # Defensive; the INSERT above is atomic under the PK.
            raise RuntimeError("failed to create strategy session anchor")
        return str(row[0])

    def load_session_anchor(self, instrument_id: str, base_timeframe: str) -> str | None:
        """Read a stable grouping anchor without accidentally creating one."""

        with self._connect() as db:
            row = db.execute(
                """SELECT anchor_time FROM strategy_session_anchors
                WHERE instrument_id=? AND base_timeframe=?""",
                (instrument_id, base_timeframe),
            ).fetchone()
        return None if row is None else str(row[0])

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

    def load_completed_run(self, run_id: str) -> Mapping[str, Any] | None:
        """Return an already-finalized daily commit without changing state.

        The staged workflow writes its filesystem receipt after the SQLite
        transaction has completed.  If a process dies in that small interval,
        this existing-table query lets commit recreate the receipt from the
        authoritative run/instrument payloads instead of re-applying cursors,
        events, or outbox rows.
        """

        with self._connect() as db:
            run = db.execute(
                """SELECT started_at, completed_at, status, updated, unchanged,
                    blocked, stale, failed, signals_detected, signals_new,
                    signals_duplicate, error_count, delivery_errors,
                    recovered_deliveries
                FROM daily_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if run is None or run["completed_at"] is None or str(run["status"]) in {
                "RUNNING",
                "INTERRUPTED",
            }:
                return None
            rows = db.execute(
                """SELECT symbol, payload_json FROM daily_run_instruments
                WHERE run_id=? ORDER BY symbol""",
                (run_id,),
            ).fetchall()
        instruments: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, Mapping):
                raise ValueError(f"daily run instrument payload is invalid for {run_id}")
            symbol = str(row["symbol"])
            if symbol in instruments:
                raise ValueError(f"daily run contains duplicate instrument payload for {symbol}")
            instruments[symbol] = payload
        return {
            "started_at": str(run["started_at"]),
            "finished_at": str(run["completed_at"]),
            "summary": {
                "selected": len(instruments),
                "updated": int(run["updated"]),
                "unchanged": int(run["unchanged"]),
                "blocked": int(run["blocked"]),
                "stale": int(run["stale"]),
                "failed": int(run["failed"]),
                "signals_detected": int(run["signals_detected"]),
                "signals_new": int(run["signals_new"]),
                "signals_duplicate": int(run["signals_duplicate"]),
                "signals_notified": 0,
                "delivery_errors": int(run["delivery_errors"]),
                "recovered_deliveries": int(run["recovered_deliveries"]),
                "error_count": int(run["error_count"]),
            },
            "instruments": instruments,
        }

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


def _resolve_notification_ids(
    events: Collection[SignalEvent],
    *,
    enqueue_notifications: bool,
    notification_event_ids: Collection[str] | None,
    notification_signal_ids: Collection[str] | None,
) -> frozenset[str]:
    """Resolve selectors while enforcing execution-event-only outbox semantics."""

    execution_ids = _coerce_id_set(
        notification_event_ids, "notification_event_ids"
    )
    legacy_ids = _coerce_id_set(
        notification_signal_ids, "notification_signal_ids"
    )
    if execution_ids is not None and legacy_ids is not None and execution_ids != legacy_ids:
        raise ValueError(
            "notification_event_ids and notification_signal_ids must identify the same events"
        )
    selected = (
        execution_ids
        if execution_ids is not None
        else legacy_ids
        if legacy_ids is not None
        else frozenset(
            event.signal_id
            for event in events
            if _is_execution_notification_event(event)
        )
        if enqueue_notifications
        else frozenset()
    )
    known_ids = {event.signal_id for event in events}
    if not selected.issubset(known_ids):
        unknown = sorted(selected - known_ids)
        raise ValueError(f"notification event IDs are not present in events: {unknown}")
    return selected if enqueue_notifications else frozenset()


def _coerce_id_set(
    values: Collection[str] | None,
    name: str,
) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a collection of event IDs")
    return frozenset(str(value) for value in values)


def _validate_execution_notification_events(
    events: Collection[SignalEvent], notification_ids: Collection[str]
) -> None:
    selected = {event.signal_id: event for event in events if event.signal_id in notification_ids}
    invalid = sorted(
        event.signal_type
        for event in selected.values()
        if not _is_execution_notification_event(event)
    )
    if invalid:
        raise ValueError(
            "notification selectors may select only confirmed execution decision events: "
            f"{invalid}"
        )


def _is_execution_notification_event(event: SignalEvent) -> bool:
    return (
        event.indicator_name == "execution_decision"
        and event.signal_type in {"ENTRY_DECISION_LONG", "ENTRY_DECISION_SHORT"}
    )


def _daily_run_summary(rows: list[Mapping[str, Any]], signals_new: int) -> dict[str, int]:
    """Mirror the established daily summary without recalculating evidence."""

    statuses = [str(row.get("run_status", "failed")) for row in rows]
    return {
        "selected": len(rows),
        "updated": statuses.count("updated"),
        "unchanged": statuses.count("unchanged"),
        "blocked": statuses.count("blocked"),
        "stale": statuses.count("stale"),
        "failed": statuses.count("failed"),
        "scanned": sum(
            1
            for row in rows
            if isinstance(row.get("scan", {}).get("status"), Mapping)
        ),
        "signals_detected": sum(int(row.get("signals_detected", 0)) for row in rows),
        "signals_new": signals_new,
        "signals_duplicate": sum(int(row.get("signals_duplicate", 0)) for row in rows),
        "signals_notified": 0,
        "delivery_errors": 0,
        "recovered_deliveries": 0,
        "error_count": sum(
            1 for row in rows if row.get("run_status") in {"failed", "blocked", "stale"}
        ),
    }


def _terminal_run_status(summary: Mapping[str, Any]) -> str:
    if summary.get("delivery_errors", 0):
        return "COMPLETED_WITH_DELIVERY_ERRORS"
    if any(summary.get(key, 0) for key in ("failed", "blocked", "stale")):
        return "FAILED"
    return "COMPLETED"


__all__ = ["SQLiteDailySignalRepository"]
