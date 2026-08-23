"""Filesystem adapter that publishes authoritative daily-run artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from inv_trend.application.daily.artifact_publication import DailyArtifactPublication
    from inv_trend.application.daily.ports import DailyReportRendererPort


CANONICAL_DIR = "01_canonical"
REPORT_DIR = "02_report"
EXPORT_DIR = "03_exports"
AUDIT_DIR = "04_audit"


class DailyRunArtifactWriter:
    """Write the JSON authority first, then create derived presentation files.

    The writer receives already calculated stage results.  It never invokes a
    strategy, reads market data, or modifies the signal store.
    """

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        signal_log_root: str | Path,
        renderer: "DailyReportRendererPort | None" = None,
    ) -> None:
        self.artifact_root = Path(artifact_root)
        self.signal_log_root = Path(signal_log_root)
        self._renderer = renderer

    def publish(
        self,
        snapshot: Mapping[str, Any],
        *,
        render_html: bool = True,
    ) -> DailyArtifactPublication:
        """Publish one immutable run and refresh its convenience projections.

        ``render_html`` is fixed when the run is first promoted.  A retry with
        the same ``run_id`` must use that same mode; callers that need a
        different derivative set must publish a new run.  This prevents a
        retry from pairing a current JSON projection with an HTML report from
        a different publication contract.
        """

        # ``render_html`` is part of a run's immutable publication contract.
        # Normalise it once so a retry cannot accidentally persist a truthy
        # non-bool value that JSON would later render differently.
        render_html = bool(render_html)
        report_date = str(snapshot["report_date"])
        run_id = str(snapshot["run_id"])
        date_root = self.artifact_root / "runs" / report_date
        date_root.mkdir(parents=True, exist_ok=True)
        final_root = date_root / run_id
        # The directory promotion is atomic.  A process may nevertheless fail
        # after that promotion and before it refreshes ``latest`` or the flat
        # compatibility copies.  Treat an already promoted immutable run as a
        # resumable publication instead of rejecting the operator's retry.
        if final_root.exists():
            return self._recover_existing(snapshot, final_root, render_html=render_html)

        temporary_root = Path(mkdtemp(prefix=f".{run_id}.", dir=date_root))
        complete_results: dict[str, Path] = {}
        complete_payloads: dict[str, Mapping[str, Any]] = {}
        try:
            for row in snapshot.get("symbols", []):
                if not isinstance(row, Mapping):
                    continue
                symbol = str(row.get("symbol") or "UNKNOWN")
                complete = _complete_result(snapshot, row)
                symbol_root = temporary_root / symbol
                complete_path = self._write_symbol(
                    symbol_root,
                    snapshot=snapshot,
                    row=row,
                    complete=complete,
                    render_html=render_html,
                )
                complete_results[symbol] = complete_path
                complete_payloads[symbol] = _read_complete_result(complete_path)
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
        compatibility_html: Path | None = None
        if render_html:
            compatibility_html = compatibility_json.with_suffix(".html")
            # Clear the old derivative before replacing its JSON source.  A
            # process interruption can leave the new JSON without HTML, which
            # is safely recoverable; it must never expose new JSON alongside
            # an older report.
            _remove_file(compatibility_html)
            _atomic_write_json(compatibility_json, snapshot)
            # Keep the legacy aggregate HTML, but build it by re-hydrating the
            # just-written canonical payloads.  HTML is deliberately never a
            # second calculation path.
            _atomic_write_text(
                compatibility_html,
                self._render_complete_analyses(complete_payloads),
            )
        else:
            # A later ``--no-html`` run must not leave a same-date legacy HTML
            # file that describes an earlier JSON compatibility snapshot.
            _remove_file(compatibility_json.with_suffix(".html"))
            _atomic_write_json(compatibility_json, snapshot)
        return _publication(
            run_directory=final_root,
            compatibility_json=compatibility_json,
            compatibility_html=compatibility_html,
            complete_results=final_complete,
        )

    def _recover_existing(
        self,
        snapshot: Mapping[str, Any],
        final_root: Path,
        *,
        render_html: bool,
    ) -> DailyArtifactPublication:
        """Complete non-atomic post-promotion work for an existing run ID."""

        complete_results: dict[str, Path] = {}
        complete_payloads: dict[str, Mapping[str, Any]] = {}
        archived_render_html: bool | None = None
        for row in snapshot.get("symbols", []):
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "UNKNOWN")
            complete_path = final_root / symbol / CANONICAL_DIR / "complete_analysis_result.json"
            if not complete_path.is_file():
                raise FileExistsError(
                    f"existing daily run is incomplete and cannot be recovered: {final_root}"
                )
            manifest = self._validate_recovery_identity(snapshot, row, complete_path)
            self._validate_canonical_artifact_hashes(complete_path)
            manifest_render_html = _manifest_render_html(manifest, complete_path)
            if archived_render_html is None:
                archived_render_html = manifest_render_html
            elif archived_render_html != manifest_render_html:
                raise ValueError(
                    "existing daily run has inconsistent render_html modes across symbols"
                )
            complete_results[symbol] = complete_path
            complete_payloads[symbol] = _read_complete_result(complete_path)

        if archived_render_html is None:
            raise FileExistsError(
                f"existing daily run has no symbol manifest and cannot be recovered: {final_root}"
            )
        if archived_render_html != render_html:
            raise ValueError(
                "existing daily run was published with a different render_html mode; "
                "retry with the original mode or use a new run_id"
            )
        self._validate_render_artifacts(complete_results, render_html=archived_render_html)

        self._update_latest(snapshot, complete_results, render_html=render_html)
        report_date = str(snapshot["report_date"])
        compatibility_json = self.artifact_root / f"{report_date}.json"
        compatibility_html: Path | None = None
        if render_html:
            compatibility_html = compatibility_json.with_suffix(".html")
            _remove_file(compatibility_html)
            _atomic_write_json(compatibility_json, snapshot)
            _atomic_write_text(
                compatibility_html,
                self._render_complete_analyses(complete_payloads),
            )
        else:
            # Remove the old derivative before replacing the JSON it may have
            # described.  A crash can leave an older JSON without HTML, but
            # never a newer JSON paired with stale HTML; a same-mode retry
            # then completes the remaining projection refresh.
            _remove_file(compatibility_json.with_suffix(".html"))
            _atomic_write_json(compatibility_json, snapshot)
        return _publication(
            run_directory=final_root,
            compatibility_json=compatibility_json,
            compatibility_html=compatibility_html,
            complete_results=complete_results,
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
        canonical_complete = _read_complete_result(complete_path)

        _write_csv(
            exports / "strategy_conditions.csv",
            _rows(canonical_complete.get("strategy_screening", {}).get("conditions")),
            (
                "condition_id", "name", "actual_value", "reference_value", "operator",
                "passed", "relative_position", "weight", "timestamp", "price",
            ),
        )
        _write_csv(
            exports / "signals.csv",
            _rows(canonical_complete.get("signals")),
            (
                "signal_id", "signal_time", "indicator_name", "indicator", "event",
                "direction", "trigger_price", "reference_value", "signal_type",
            ),
        )
        _write_csv(
            exports / "anomalies.csv",
            _rows(canonical_complete.get("anomalies")),
            ("anomaly_id", "timestamp", "anomaly_type", "severity", "price", "summary"),
        )
        _write_csv(
            exports / "state_transitions.csv",
            _rows(canonical_complete.get("state_transitions")),
            ("transition_id", "timestamp", "state_name", "from_state", "to_state", "price", "reason"),
        )

        if render_html:
            _write_text(
                report / "trend_analysis_report.html",
                self._render_complete_analysis(canonical_complete),
            )

        _write_json(
            audit / "run_manifest.json",
            _run_manifest(snapshot, row, self.signal_log_root, render_html=render_html),
        )
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

    def _render_complete_analysis(self, complete: Mapping[str, Any]) -> str:
        """Delegate presentation to the injected observability port."""

        return _rendered_html(
            self._html_renderer().render_complete_analysis(complete),
            operation="render_complete_analysis",
        )

    def _render_complete_analyses(
        self,
        completes: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """Render the flat compatibility page from canonical payloads only."""

        return _rendered_html(
            self._html_renderer().render_complete_analyses(completes),
            operation="render_complete_analyses",
        )

    def _html_renderer(self) -> "DailyReportRendererPort":
        """Return an injected renderer or lazily preserve the legacy default.

        New composition roots should always inject this dependency.  The lazy
        fallback is retained solely for the long-standing public
        ``DailyRunArtifactWriter(...)`` constructor; it asks the dedicated
        adapter composition module for the default and never imports an
        observability implementation from this filesystem adapter.
        """

        if self._renderer is None:
            from .composition import create_daily_report_renderer

            self._renderer = create_daily_report_renderer()
        return self._renderer

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
            report_path = complete_path.parent.parent / REPORT_DIR / "trend_analysis_report.html"
            if render_html:
                if not report_path.is_file():
                    raise FileExistsError(
                        "published run is missing its required HTML report: "
                        f"{report_path}"
                    )
                _atomic_copy(complete_path, latest / "complete_analysis_result.json")
                _atomic_copy(report_path, latest / "trend_analysis_report.html")
            else:
                # ``latest`` is a convenience projection, not archival
                # evidence.  Remove a stale report instead of pairing it with
                # a newer JSON that was explicitly published without HTML.
                _remove_file(latest / "trend_analysis_report.html")
                _atomic_copy(complete_path, latest / "complete_analysis_result.json")

    @staticmethod
    def _validate_render_artifacts(
        complete_results: Mapping[str, Path], *, render_html: bool
    ) -> None:
        """Verify that immutable derived reports match the manifest mode.

        A run promoted before a process crash may be retried safely.  A run
        whose archived HTML disagrees with its immutable manifest is corrupt,
        however, and must not be allowed to refresh ``latest`` or the flat
        compatibility projections.
        """

        for complete_path in complete_results.values():
            report_path = complete_path.parent.parent / REPORT_DIR / "trend_analysis_report.html"
            has_report = report_path.is_file()
            if render_html and not has_report:
                raise FileExistsError(
                    "existing daily run is missing its required HTML report: "
                    f"{report_path}"
                )
            if not render_html and has_report:
                raise ValueError(
                    "existing daily run contains an HTML report despite its "
                    f"immutable no-HTML mode: {report_path}"
                )

    def _validate_recovery_identity(
        self,
        snapshot: Mapping[str, Any],
        row: Mapping[str, Any],
        complete_path: Path,
    ) -> Mapping[str, Any]:
        """Refuse to reuse an immutable run ID for a different snapshot."""

        manifest_path = complete_path.parent.parent / AUDIT_DIR / "run_manifest.json"
        if not manifest_path.is_file():
            raise FileExistsError(
                f"existing daily run is missing its identity manifest: {manifest_path.parent.parent}"
            )
        manifest = _read_complete_result(manifest_path)
        expected = _payload_hash(snapshot)
        actual = str(manifest.get("snapshot_hash") or "")
        if actual != expected:
            raise ValueError(
                "existing daily run has a different immutable snapshot; use a new run_id"
            )
        if str(manifest.get("symbol") or "") != str(row.get("symbol") or ""):
            raise ValueError("existing daily run manifest has an invalid symbol identity")
        return manifest

    @staticmethod
    def _validate_canonical_artifact_hashes(complete_path: Path) -> None:
        """Refuse recovery when archived authority files no longer match audit.

        Promotion is intentionally immutable, but a later filesystem edit must
        never be re-published into ``latest`` or the flat compatibility copy
        merely because a matching run manifest still exists.  The hash ledger
        is written with the four canonical JSON files and is therefore the
        first integrity gate on every recovery attempt.
        """

        canonical = complete_path.parent
        audit_path = canonical.parent / AUDIT_DIR / "artifact_hashes.json"
        if not audit_path.is_file():
            raise FileExistsError(
                "existing daily run is missing its canonical hash ledger: "
                f"{audit_path}"
            )
        ledger = _read_complete_result(audit_path)
        filenames = (
            "data_update_result.json",
            "strategy_screening_result.json",
            "trend_decision_result.json",
            "complete_analysis_result.json",
        )
        for filename in filenames:
            artifact = canonical / filename
            expected = str(ledger.get(filename) or "")
            actual = _sha256(artifact) if artifact.is_file() else ""
            if not expected or actual != expected:
                raise ValueError(
                    "existing daily run canonical artifact hash mismatch: "
                    f"{artifact}"
                )

        complete = _read_complete_result(complete_path)
        hashes = _mapping(complete.get("hashes"))
        if not bool(hashes.get("hash_chain_valid")):
            raise ValueError(
                "existing daily run complete analysis has an invalid stage hash chain"
            )


def _publication(**kwargs: Any) -> "DailyArtifactPublication":
    """Resolve the application result DTO after composition has completed.

    The legacy ``application`` package still eagerly imports its compatibility
    surface.  Deferring only this DTO lookup keeps that surface importable
    while the concrete adapter remains outside the application package.
    """

    from inv_trend.application.daily.artifact_publication import DailyArtifactPublication

    return DailyArtifactPublication(**kwargs)


def _rendered_html(value: Any, *, operation: str) -> str:
    """Fail clearly when a presentation adapter violates the renderer port."""

    if not isinstance(value, str):
        raise TypeError(f"DailyReportRendererPort.{operation} must return str")
    return value


def _read_complete_result(path: Path) -> Mapping[str, Any]:
    """Load the canonical JSON used as the sole input to derived artifacts."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"complete analysis is not a JSON object: {path}")
    return payload


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
            "report_date": snapshot.get("report_date"),
            "report_summary": _mapping(snapshot.get("summary")),
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
    snapshot: Mapping[str, Any],
    row: Mapping[str, Any],
    signal_log_root: Path,
    *,
    render_html: bool,
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
        # This binds retry recovery to the complete commit snapshot.  The
        # value is redundant with the stage hashes, but covers compatibility
        # projections such as signal IDs, report bundles, and state changes.
        "snapshot_hash": _payload_hash(snapshot),
        # A retry must preserve the original set of derived artifacts.  In
        # particular, a no-HTML run cannot later create a compatibility HTML
        # file or leave a stale ``latest`` report behind.
        "render_html": render_html,
        "signal_log_root": str(signal_log_root),
        "delivery": {
            "signals_new": row.get("signals_new", 0),
            "signals_detected": row.get("signals_detected", 0),
        },
    }


def _manifest_render_html(manifest: Mapping[str, Any], complete_path: Path) -> bool:
    """Read the immutable derived-artifact mode from an archived manifest."""

    value = manifest.get("render_html")
    if not isinstance(value, bool):
        raise FileExistsError(
            "existing daily run is missing its immutable render_html mode and "
            f"cannot be recovered safely: {complete_path.parent.parent}"
        )
    return value


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


# Preferred adapter-facing name; the writer name remains the public legacy API.
DailyArtifactPublisherAdapter = DailyRunArtifactWriter


__all__ = ["DailyArtifactPublisherAdapter", "DailyRunArtifactWriter"]
