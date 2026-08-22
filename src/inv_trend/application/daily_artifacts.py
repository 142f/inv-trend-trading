"""Publish authoritative daily-run artifacts without changing trading state."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import mkdtemp
from typing import Any, Mapping


CANONICAL_DIR = "01_canonical"
REPORT_DIR = "02_report"
EXPORT_DIR = "03_exports"
AUDIT_DIR = "04_audit"


@dataclass(frozen=True)
class DailyArtifactPublication:
    """Locations published for a single daily-run snapshot."""

    run_directory: Path
    compatibility_json: Path
    compatibility_html: Path | None
    complete_results: Mapping[str, Path]


class DailyRunArtifactWriter:
    """Write the JSON authority first, then create derived presentation files.

    The writer receives already calculated stage results.  It never invokes a
    strategy, reads market data, or modifies the signal store.
    """

    def __init__(self, artifact_root: str | Path, *, signal_log_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root)
        self.signal_log_root = Path(signal_log_root)

    def publish(
        self,
        snapshot: Mapping[str, Any],
        *,
        render_html: bool = True,
    ) -> DailyArtifactPublication:
        report_date = str(snapshot["report_date"])
        run_id = str(snapshot["run_id"])
        date_root = self.artifact_root / "runs" / report_date
        date_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(mkdtemp(prefix=f".{run_id}.", dir=date_root))
        final_root = date_root / run_id
        complete_results: dict[str, Path] = {}
        complete_payloads: dict[str, Mapping[str, Any]] = {}
        try:
            for row in snapshot.get("symbols", []):
                if not isinstance(row, Mapping):
                    continue
                symbol = str(row.get("symbol") or "UNKNOWN")
                complete = _complete_result(snapshot, row)
                complete_payloads[symbol] = complete
                symbol_root = temporary_root / symbol
                complete_results[symbol] = self._write_symbol(
                    symbol_root,
                    snapshot=snapshot,
                    row=row,
                    complete=complete,
                    render_html=render_html,
                )
            if final_root.exists():  # run IDs are UUIDs; refusing overwrite protects audit history.
                raise FileExistsError(f"daily run artifact already exists: {final_root}")
            os.replace(temporary_root, final_root)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise

        final_complete = {
            symbol: final_root / path.relative_to(temporary_root)
            for symbol, path in complete_results.items()
        }
        self._update_latest(snapshot, final_complete, render_html=render_html)

        compatibility_json = self.artifact_root / f"{report_date}.json"
        _atomic_write_json(compatibility_json, snapshot)
        compatibility_html: Path | None = None
        if render_html:
            compatibility_html = compatibility_json.with_suffix(".html")
            # Keep the legacy aggregate HTML, but build it by re-hydrating the
            # just-written canonical payloads.  HTML is deliberately never a
            # second calculation path.
            _atomic_write_text(
                compatibility_html,
                _dashboard_html(_snapshot_from_completes(snapshot, complete_payloads)),
            )
        return DailyArtifactPublication(
            run_directory=final_root,
            compatibility_json=compatibility_json,
            compatibility_html=compatibility_html,
            complete_results=final_complete,
        )

    def _write_symbol(
        self,
        root: Path,
        *,
        snapshot: Mapping[str, Any],
        row: Mapping[str, Any],
        complete: Mapping[str, Any],
        render_html: bool,
    ) -> Path:
        canonical = root / CANONICAL_DIR
        report = root / REPORT_DIR
        exports = root / EXPORT_DIR
        audit = root / AUDIT_DIR
        data_update = _stage(row, "data_update")
        screening = _stage(row, "strategy_screening")
        decision = _stage(row, "trend_decision")
        _write_json(canonical / "data_update_result.json", data_update)
        _write_json(canonical / "strategy_screening_result.json", screening)
        _write_json(canonical / "trend_decision_result.json", decision)
        complete_path = canonical / "complete_analysis_result.json"
        _write_json(complete_path, complete)

        _write_csv(
            exports / "strategy_conditions.csv",
            _rows(complete.get("strategy_screening", {}).get("conditions")),
            (
                "condition_id", "name", "actual_value", "reference_value", "operator",
                "passed", "relative_position", "weight", "timestamp", "price",
            ),
        )
        _write_csv(
            exports / "signals.csv",
            _rows(complete.get("signals")),
            (
                "signal_id", "signal_time", "indicator_name", "indicator", "event",
                "direction", "trigger_price", "reference_value", "signal_type",
            ),
        )
        _write_csv(
            exports / "anomalies.csv",
            _rows(complete.get("anomalies")),
            ("anomaly_id", "timestamp", "anomaly_type", "severity", "price", "summary"),
        )
        _write_csv(
            exports / "state_transitions.csv",
            _rows(complete.get("state_transitions")),
            ("transition_id", "timestamp", "state_name", "from_state", "to_state", "price", "reason"),
        )

        if render_html:
            _write_text(report / "trend_analysis_report.html", _dashboard_html(_snapshot_from_complete(complete)))

        _write_json(audit / "run_manifest.json", _run_manifest(snapshot, row, self.signal_log_root))
        _write_json(
            audit / "configuration_snapshot.json",
            _configuration_snapshot(snapshot, data_update, screening),
        )
        _write_json(audit / "data_lineage.json", _data_lineage(row, data_update))
        canonical_hashes = {
            "data_update_result.json": _sha256(canonical / "data_update_result.json"),
            "strategy_screening_result.json": _sha256(canonical / "strategy_screening_result.json"),
            "trend_decision_result.json": _sha256(canonical / "trend_decision_result.json"),
            "complete_analysis_result.json": _sha256(complete_path),
        }
        _write_json(audit / "artifact_hashes.json", canonical_hashes)
        return complete_path

    def _update_latest(
        self,
        snapshot: Mapping[str, Any],
        complete_results: Mapping[str, Path],
        *,
        render_html: bool,
    ) -> None:
        rows = {
            str(row.get("symbol")): row
            for row in snapshot.get("symbols", [])
            if isinstance(row, Mapping)
        }
        for symbol, complete_path in complete_results.items():
            row = rows.get(symbol, {})
            if str(row.get("run_status")) not in {"updated", "unchanged"}:
                continue
            latest = self.artifact_root / "latest" / symbol
            _atomic_copy(complete_path, latest / "complete_analysis_result.json")
            report_path = complete_path.parent.parent / REPORT_DIR / "trend_analysis_report.html"
            if render_html and report_path.exists():
                _atomic_copy(report_path, latest / "trend_analysis_report.html")


def _complete_result(snapshot: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    data_update = _stage(row, "data_update")
    screening = _stage(row, "strategy_screening")
    decision = _stage(row, "trend_decision")
    report_bundle = _mapping(row.get("report_bundle"))
    row_data = _mapping(row.get("data"))
    signals = _rows(row.get("signals")) or _rows(report_bundle.get("signals"))
    hashes = {
        "data_update_result_hash": data_update.get("result_hash"),
        "strategy_screening_result_hash": screening.get("result_hash"),
        "trend_decision_result_hash": decision.get("result_hash"),
        "strategy_input_data_hash": screening.get("input_data_hash"),
        "trend_input_data_hash": decision.get("input_data_hash"),
        "trend_input_screening_hash": decision.get("input_screening_hash"),
    }
    hashes["hash_chain_valid"] = bool(
        hashes["strategy_input_data_hash"] == hashes["data_update_result_hash"]
        and hashes["trend_input_data_hash"] == hashes["data_update_result_hash"]
        and hashes["trend_input_screening_hash"] == hashes["strategy_screening_result_hash"]
    )
    return {
        "schema_version": str(snapshot.get("schema_version", "4")),
        "report_schema_version": str(snapshot.get("report_schema_version", "3")),
        "metadata": {
            "symbol": row.get("symbol"),
            "instrument_id": row.get("instrument_id"),
            "timeframe": row.get("timeframe", "D1"),
            "dataset_version": data_update.get("dataset_version") or row_data.get("dataset_version"),
            "as_of": decision.get("as_of") or data_update.get("latest_complete_d1"),
            "run_status": row.get("run_status"),
            "snapshot_schema_version": snapshot.get("schema_version"),
            "report_schema_version": snapshot.get("report_schema_version"),
        },
        "data_update": data_update,
        "strategy_screening": screening,
        "trend_decision": decision,
        "signals": signals,
        "anomalies": _rows(report_bundle.get("anomalies")),
        "state_transitions": _rows(report_bundle.get("state_transitions")),
        "report_bundle": report_bundle,
        "hashes": hashes,
    }


def _stage(row: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    direct = row.get(f"{name}_result")
    if isinstance(direct, Mapping):
        return direct
    stages = _mapping(row.get("stages"))
    value = stages.get(name)
    return value if isinstance(value, Mapping) else {}


def _snapshot_from_complete(complete: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(complete.get("metadata"))
    bundle = _mapping(complete.get("report_bundle"))
    row = {
        "symbol": metadata.get("symbol"),
        "instrument_id": metadata.get("instrument_id"),
        "timeframe": metadata.get("timeframe", "D1"),
        "run_status": metadata.get("run_status", "updated"),
        "data_update_result": complete.get("data_update", {}),
        "strategy_screening_result": complete.get("strategy_screening", {}),
        "trend_decision_result": complete.get("trend_decision", {}),
        "report_bundle": bundle,
    }
    return {
        "schema_version": "4",
        "report_schema_version": "3",
        "report_date": str(metadata.get("as_of") or "")[:10],
        "symbols": [row],
        "summary": bundle.get("summary", {}),
    }


def _snapshot_from_completes(
    snapshot: Mapping[str, Any], completes: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Rebuild a dashboard input solely from canonical aggregate payloads."""

    symbols: list[Mapping[str, Any]] = []
    for original_row in _rows(snapshot.get("symbols")):
        symbol = str(original_row.get("symbol") or "")
        complete = completes.get(symbol)
        if complete is None:
            continue
        symbols.extend(_snapshot_from_complete(complete).get("symbols", ()))
    return {
        "schema_version": snapshot.get("schema_version", "4"),
        "report_schema_version": snapshot.get("report_schema_version", "3"),
        "report_date": snapshot.get("report_date"),
        "symbols": symbols,
        "summary": _mapping(snapshot.get("summary")),
    }


def _configuration_snapshot(
    snapshot: Mapping[str, Any],
    data_update: Mapping[str, Any],
    screening: Mapping[str, Any],
) -> dict[str, Any]:
    """Store resolved configuration plus stable hashes of the three contracts."""

    configuration = dict(_mapping(snapshot.get("configuration")))
    data_contract = {
        "quality_required": "CURATED",
        "freshness_required": "passed",
        "lineage_required": "verified",
        "timeframe": data_update.get("timeframe", "D1"),
    }
    configuration["data_config_hash"] = _payload_hash(data_contract)
    configuration["strategy_config_hash"] = screening.get("configuration_hash") or _payload_hash(
        _mapping(configuration.get("daily_checks"))
    )
    configuration["decision_config_hash"] = _payload_hash(
        _mapping(configuration.get("trend_decision"))
    )
    return configuration


def _run_manifest(
    snapshot: Mapping[str, Any], row: Mapping[str, Any], signal_log_root: Path
) -> dict[str, Any]:
    return {
        "run_id": snapshot.get("run_id"),
        "report_date": snapshot.get("report_date"),
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "symbol": row.get("symbol"),
        "instrument_id": row.get("instrument_id"),
        "status": row.get("run_status"),
        "strategy_version": _mapping(snapshot.get("configuration")).get("strategy_version"),
        "signal_log_root": str(signal_log_root),
        "delivery": {
            "signals_new": row.get("signals_new", 0),
            "signals_detected": row.get("signals_detected", 0),
        },
    }


def _data_lineage(row: Mapping[str, Any], data_update: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "instrument_id": row.get("instrument_id"),
        "provider": row.get("provider", {}),
        "dataset_version": data_update.get("dataset_version"),
        "latest_complete_d1": data_update.get("latest_complete_d1"),
        "quality": data_update.get("quality", {}),
        "freshness": data_update.get("freshness", {}),
        "lineage": data_update.get("lineage", {}),
    }


def _dashboard_html(snapshot: Mapping[str, Any]) -> str:
    # Local import prevents the application orchestration layer from becoming a
    # renderer dependency at module import time.
    from inv_trend.observability.report_renderer import render_daily_dashboard

    return render_daily_dashboard(snapshot)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _payload_hash(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (tuple, list)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


__all__ = ["DailyArtifactPublication", "DailyRunArtifactWriter"]
