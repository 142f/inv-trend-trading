"""Root and batch navigation indexes for published daily artifacts.

Single responsibility: render the result, batch and output indexes from the run
snapshot; file writing is delegated to artifact_io.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .artifact_io import _atomic_write_json, _write_json
from .artifact_layout import (
    AUDIT_DIR,
    CANONICAL_DIR,
    EXPORT_DIR,
    LAYOUT_VERSION,
    REPORT_DIR,
    SUMMARY_DIR,
)
from .artifact_paths import _path_segment

def _write_symbol_index(
    root: Path,
    snapshot: Mapping[str, Any],
    row: Mapping[str, Any],
    named_paths: Mapping[str, Path],
) -> Path:
    path = root / "结果索引.json"
    payload = {
        "layout_version": LAYOUT_VERSION,
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe", "D1"),
        "report_date": snapshot.get("report_date"),
        "run_id": snapshot.get("run_id"),
        "report_schema_version": snapshot.get("report_schema_version", "5"),
        "result_types": {
            key: value.relative_to(root).as_posix()
            for key, value in named_paths.items()
        },
        "compatibility_entries": {
            "complete_analysis": f"{CANONICAL_DIR}/complete_analysis_result.json",
            "html_report": f"{REPORT_DIR}/trend_analysis_report.html",
            "exports": EXPORT_DIR,
            "audit": AUDIT_DIR,
        },
    }
    _write_json(path, payload)
    return path


def _write_batch_index(
    root: Path,
    snapshot: Mapping[str, Any],
    complete_results: Mapping[str, Path],
) -> Path:
    rows = {
        str(row.get("symbol")): row
        for row in snapshot.get("symbols", [])
        if isinstance(row, Mapping)
    }
    symbols = []
    for symbol, complete_path in complete_results.items():
        row = rows.get(symbol, {})
        symbol_root = complete_path.parent.parent
        symbols.append(
            {
                "symbol": symbol,
                "timeframe": row.get("timeframe", "D1"),
                "directory": symbol_root.relative_to(root).as_posix(),
                "result_index": (symbol_root / "结果索引.json").relative_to(root).as_posix(),
            }
        )
    path = root / "批次索引.json"
    _write_json(
        path,
        {
            "layout_version": LAYOUT_VERSION,
            "run_id": snapshot.get("run_id"),
            "report_date": snapshot.get("report_date"),
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "report_schema_version": snapshot.get("report_schema_version", "5"),
            "symbols": symbols,
        },
    )
    return path


def _write_output_index(
    artifact_root: Path,
    snapshot: Mapping[str, Any],
    *,
    run_directory: Path,
    complete_results: Mapping[str, Path],
    compatibility_json: Path,
    compatibility_html: Path | None,
    summary_json: Path,
    summary_html: Path | None,
) -> Path:
    """Refresh the small root navigation file without scanning prior runs."""

    def relative(path: Path | None) -> str | None:
        return None if path is None else path.relative_to(artifact_root).as_posix()

    latest_symbols = {
        str(row.get("symbol"))
        for row in snapshot.get("symbols", [])
        if isinstance(row, Mapping)
        and str(row.get("run_status")) in {"updated", "unchanged"}
    }
    latest = {
        symbol: {
            "directory": (
                Path("latest") / _path_segment(symbol, fallback="UNKNOWN")
            ).as_posix(),
            "complete_analysis": (
                Path("latest")
                / _path_segment(symbol, fallback="UNKNOWN")
                / "complete_analysis_result.json"
            ).as_posix(),
            "result_index": (
                Path("latest") / _path_segment(symbol, fallback="UNKNOWN") / "结果索引.json"
            ).as_posix(),
        }
        for symbol in complete_results
        if symbol in latest_symbols
    }
    path = artifact_root / "输出索引.json"
    _atomic_write_json(
        path,
        {
            "layout_version": LAYOUT_VERSION,
            "latest_batch": {
                "run_id": snapshot.get("run_id"),
                "report_date": snapshot.get("report_date"),
                "timeframe": snapshot.get("timeframe", "D1"),
                "run_directory": relative(run_directory),
                "batch_index": relative(run_directory / "批次索引.json")
                if (run_directory / "批次索引.json").is_file()
                else None,
            },
            "summary": {
                "json": relative(summary_json),
                "html": relative(summary_html),
            },
            "latest_symbols": latest,
            "compatibility": {
                "json": relative(compatibility_json),
                "html": relative(compatibility_html),
            },
            "categories": {
                "runs": "runs",
                "summaries": SUMMARY_DIR,
                "latest": "latest",
                "state": "state",
            },
        },
    )
    return path

__all__ = ["_write_batch_index", "_write_output_index", "_write_symbol_index"]
