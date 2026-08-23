"""Validation of strategy-visible dataset lineage and quality artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .integrity import sha256_file
from .models import DataLineageError
from .storage import DataLake


@dataclass(frozen=True)
class CurrentLineage:
    """Resolved and independently verified current-version lineage."""

    symbol: str
    timeframe: str
    dataset_version: str
    pointer: dict[str, Any]
    dataset_manifest_path: Path
    dataset_manifest: dict[str, Any]
    quality_report_path: Path
    quality_report: dict[str, Any]


@dataclass(frozen=True)
class VersionedLineage:
    """Verified lineage for one immutable dataset version.

    This deliberately makes no assertion about the mutable ``current``
    pointer.  Resumable workflows use it after recording their permitted
    dataset version during a preceding stage.
    """

    symbol: str
    timeframe: str
    dataset_version: str
    curated_path: Path
    dataset_manifest_path: Path
    dataset_manifest: dict[str, Any]
    quality_report_path: Path
    quality_report: dict[str, Any]


def load_versioned_lineage(
    lake: DataLake,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    *,
    bars: pd.DataFrame | None = None,
) -> VersionedLineage:
    """Verify a recorded curated version without resolving ``current``.

    The manifest, quality report and Parquet checksum are all checked so a
    pinned stage can safely continue after another process activates a newer
    version for the same symbol.
    """

    symbol, timeframe = symbol.upper(), timeframe.upper()
    version = str(dataset_version or "")
    if not version:
        raise DataLineageError(f"dataset version is required for {symbol}/{timeframe}")

    # Let the data-lake boundary validate the token before it is ever used in
    # a manifest path.  It also resolves the exact immutable artifact that
    # this lineage record must describe.
    resolved_version_path = lake.curated_version_path(symbol, timeframe, version)
    manifest_path = lake.root / "dataset_manifests" / f"{version}.json"
    manifest = _read_json(manifest_path, "dataset manifest")
    _require_equal(manifest.get("dataset_version"), version, "dataset manifest version")
    _require_equal(
        str(manifest.get("symbol", "")).upper(), symbol, "dataset manifest symbol"
    )
    _require_equal(
        str(manifest.get("timeframe", "")).upper(), timeframe, "dataset manifest timeframe"
    )

    curated_path = lake._resolve_root_relative(str(manifest.get("curated_path") or ""))
    if not curated_path.exists():
        raise DataLineageError(f"curated artifact is missing: {curated_path}")
    if not _same_path(curated_path, resolved_version_path):
        raise DataLineageError(
            "dataset manifest and versioned curated lookup reference different artifacts"
        )
    _verify_hash(curated_path, manifest.get("curated_sha256"), "curated artifact")

    report_path = lake._resolve_root_relative(
        str(manifest.get("quality_report_path") or "")
    )
    _verify_hash(report_path, manifest.get("quality_report_sha256"), "quality report")
    report = _read_json(report_path, "quality report")
    recorded_version = report.get("dataset_version") or report.get("run_id")
    _require_equal(recorded_version, version, "quality report version")
    _require_equal(
        str(report.get("symbol", "")).upper(), symbol, "quality report symbol"
    )
    _require_equal(
        str(report.get("timeframe", "")).upper(), timeframe, "quality report timeframe"
    )

    if bars is not None:
        _validate_bars(
            bars,
            symbol=symbol,
            timeframe=timeframe,
            version=version,
            manifest=manifest,
            report=report,
        )

    return VersionedLineage(
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=version,
        curated_path=curated_path,
        dataset_manifest_path=manifest_path,
        dataset_manifest=manifest,
        quality_report_path=report_path,
        quality_report=report,
    )


def load_current_lineage(
    lake: DataLake,
    symbol: str,
    timeframe: str,
    *,
    bars: pd.DataFrame | None = None,
) -> CurrentLineage:
    """Resolve current artifacts and fail closed on any lineage inconsistency."""
    symbol, timeframe = symbol.upper(), timeframe.upper()
    pointer = lake.current_version(symbol, timeframe)
    if pointer is None:
        raise DataLineageError(f"current dataset is missing for {symbol}/{timeframe}")

    version = str(pointer.get("version") or "")
    if not version:
        raise DataLineageError(f"current pointer has no version for {symbol}/{timeframe}")
    catalog_current = lake._current_catalog_row(symbol, timeframe)
    if (
        catalog_current is None
        or str(catalog_current.get("version")) != version
        or str(catalog_current.get("run_id")) != str(pointer.get("run_id"))
    ):
        raise DataLineageError(
            f"pointer/catalog drift for {symbol}/{timeframe}: "
            f"pointer={version}, catalog="
            f"{catalog_current and catalog_current.get('version')}"
        )
    manifest_ref = str(pointer.get("dataset_manifest_path") or "")
    if not manifest_ref:
        raise DataLineageError(
            f"current dataset manifest is missing for {symbol}/{timeframe}"
        )

    manifest_path = lake._resolve_root_relative(manifest_ref)
    manifest = _read_json(manifest_path, "dataset manifest")
    _require_equal(manifest.get("dataset_version"), version, "dataset manifest version")
    _require_equal(str(manifest.get("symbol", "")).upper(), symbol, "dataset manifest symbol")
    _require_equal(str(manifest.get("timeframe", "")).upper(), timeframe, "dataset manifest timeframe")

    curated_path = lake._resolve_root_relative(str(manifest.get("curated_path") or ""))
    pointer_path = lake._resolve_root_relative(str(pointer.get("path") or ""))
    if not curated_path.exists():
        raise DataLineageError(f"current curated artifact is missing: {curated_path}")
    try:
        same_target = curated_path.resolve() == pointer_path.resolve()
    except OSError:
        same_target = curated_path == pointer_path
    if not same_target:
        raise DataLineageError(
            "current pointer and dataset manifest reference different curated artifacts"
        )
    _verify_hash(curated_path, manifest.get("curated_sha256"), "curated artifact")

    report_path = lake._resolve_root_relative(
        str(manifest.get("quality_report_path") or "")
    )
    _verify_hash(report_path, manifest.get("quality_report_sha256"), "quality report")
    report = _read_json(report_path, "quality report")
    recorded_version = report.get("dataset_version") or report.get("run_id")
    _require_equal(recorded_version, version, "quality report version")
    _require_equal(
        str(report.get("symbol", "")).upper(), symbol, "quality report symbol"
    )
    _require_equal(
        str(report.get("timeframe", "")).upper(),
        timeframe,
        "quality report timeframe",
    )

    if bars is not None:
        _validate_bars(
            bars,
            symbol=symbol,
            timeframe=timeframe,
            version=version,
            manifest=manifest,
            report=report,
        )

    return CurrentLineage(
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=version,
        pointer=pointer,
        dataset_manifest_path=manifest_path,
        dataset_manifest=manifest,
        quality_report_path=report_path,
        quality_report=report,
    )


def _validate_bars(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    version: str,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    if bars.empty:
        raise DataLineageError(f"current dataset is empty for {symbol}/{timeframe}")
    if "dataset_version" not in bars:
        raise DataLineageError("current bars do not contain dataset_version")
    versions = set(bars["dataset_version"].astype(str).dropna())
    if versions != {version}:
        raise DataLineageError(
            f"current bars contain unexpected dataset versions: {sorted(versions)}"
        )
    if "symbol" in bars:
        symbols = set(bars["symbol"].astype(str).str.upper())
        if symbols != {symbol}:
            raise DataLineageError(
                f"current bars contain unexpected symbols: {sorted(symbols)}"
            )
    if "timeframe" in bars:
        timeframes = set(bars["timeframe"].astype(str).str.upper())
        if timeframes != {timeframe}:
            raise DataLineageError(
                f"current bars contain unexpected timeframes: {sorted(timeframes)}"
            )

    _require_int_equal(manifest.get("row_count"), len(bars), "dataset manifest row_count")
    report_rows = next(
        (
            report.get(key)
            for key in ("actual_bars", "stored_row_count", "row_count")
            if report.get(key) is not None
        ),
        None,
    )
    if report_rows is None:
        raise DataLineageError("quality report does not record the dataset row count")
    _require_int_equal(report_rows, len(bars), "quality report row_count")

    expected_instrument = str(manifest.get("instrument_id") or "")
    report_instrument = str(report.get("instrument_id") or "")
    if report_instrument and expected_instrument:
        _require_equal(report_instrument, expected_instrument, "quality report instrument")
    if "instrument_id" in bars and expected_instrument:
        instruments = set(bars["instrument_id"].astype(str))
        if instruments != {expected_instrument}:
            raise DataLineageError(
                "current bars instrument_id is inconsistent with dataset manifest"
            )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise DataLineageError(f"{label} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataLineageError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise DataLineageError(f"invalid {label} payload: {path}")
    return payload


def _verify_hash(path: Path, expected: object, label: str) -> None:
    if not path.exists():
        raise DataLineageError(f"{label} is missing: {path}")
    expected_text = str(expected or "")
    if not expected_text:
        raise DataLineageError(f"{label} hash is missing")
    actual = sha256_file(path)
    if actual != expected_text:
        raise DataLineageError(
            f"{label} hash mismatch: expected {expected_text}, got {actual}"
        )


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _require_equal(actual: object, expected: object, label: str) -> None:
    if str(actual) != str(expected):
        raise DataLineageError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _require_int_equal(actual: object, expected: int, label: str) -> None:
    try:
        parsed = int(actual)
    except (TypeError, ValueError) as exc:
        raise DataLineageError(f"{label} is invalid: {actual!r}") from exc
    if parsed != expected:
        raise DataLineageError(
            f"{label} mismatch: expected {expected}, got {parsed}"
        )
