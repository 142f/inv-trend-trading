"""Independent post-hoc audit of published D1 datasets.

Recomputes every statistic from the final Parquet files instead of trusting
persisted Quality Reports, then compares report, manifest, catalog, current
pointer and Repository read for consistency.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from .calendar import classify_missing
from .config import load_instruments
from .processing import FRAME_DELTAS
from .integrity import sha256_file
from .storage import DataLake


def audit_dataset(root: str | Path, symbol: str, timeframe: str = "D1") -> dict[str, object]:
    lake = DataLake(root)
    current = lake.current_version(symbol.upper(), timeframe.upper())
    if current is None:
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "NO_CURRENT_POINTER"}
    path = lake._resolve_root_relative(str(current["path"]))
    if not path.exists():
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "CURATED_FILE_MISSING",
                "expected_path": str(path)}
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    stored = len(frame)
    complete = int(frame["is_complete"].astype(bool).sum())
    complete_view = frame.loc[frame["is_complete"].astype(bool)]
    actual_start = frame["timestamp"].min()
    actual_end = frame["timestamp"].max()

    instruments = load_instruments()
    instrument = instruments.get(symbol.upper())
    session = instrument.session if instrument else ""
    market = instrument.market if instrument else str(frame["market"].iloc[0]) if "market" in frame else ""
    version = current["version"]
    dataset_manifest_ref = current.get("dataset_manifest_path")
    manifest_path = (
        lake._resolve_root_relative(str(dataset_manifest_ref))
        if dataset_manifest_ref
        else lake.root / "dataset_manifests" / f"{version}.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    report_path = (
        lake._resolve_root_relative(str(manifest.get("quality_report_path")))
        if manifest and manifest.get("quality_report_path")
        else lake.root / "quality_reports" / f"{version}.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None

    # Independent expected-window reconstruction.  Never reuses the persisted
    # Quality Report's missing/coverage numbers; recomputes from
    # immutable dataset coverage range + trading calendar.  An ingestion run's
    # short requested range is deliberately not part of this calculation.
    requested_start = manifest.get("full_actual_start") if manifest else None
    requested_end = manifest.get("full_actual_end") if manifest else None
    listing_start = instrument.earliest_valid_date if instrument else None
    provider_available_start = None

    expected_start = None
    expected_end = None
    if requested_start is not None and requested_end is not None:
        try:
            req_start = pd.Timestamp(requested_start).tz_convert("UTC")
            settled = pd.Timestamp(requested_end).tz_convert("UTC")
            expected_end = settled
            expected_start = req_start
        except (ValueError, TypeError):
            expected_start = expected_end = None

    if expected_start is not None and expected_end is not None and expected_start <= expected_end:
        absent, breakdown = classify_missing(
            pd.DatetimeIndex(frame["timestamp"].drop_duplicates()),
            market=market, session=session,
            start=expected_start, end=expected_end, freq=FRAME_DELTAS[timeframe],
        )
        theoretical = len(pd.date_range(expected_start, expected_end, freq=FRAME_DELTAS[timeframe]))
        if session in {"24x5", "regular"}:
            theoretical -= breakdown["weekend"]
        if session == "regular" and market in {"xnas", "xnys"}:
            theoretical -= breakdown["holiday"]
        missing = breakdown["provider_gap"]
    else:
        theoretical = 0
        missing = None
        breakdown = {"weekend": 0, "holiday": 0, "provider_gap": 0, "unknown": 0}
        expected_start = expected_end = None

    duplicated = frame["timestamp"].duplicated(keep=False)
    exact_duplicates = 0
    conflicting_duplicates = 0
    if duplicated.any():
        ohlc_cols = [c for c in ("open", "high", "low", "close", "volume") if c in frame]
        for _, group in frame.loc[duplicated].groupby(frame.loc[duplicated, "timestamp"]):
            values = group[ohlc_cols].astype(float)
            if (values - values.iloc[0]).abs().max().max() <= 1e-9:
                exact_duplicates += 1
            else:
                conflicting_duplicates += 1

    invalid_ohlc = 0
    negative_volume = 0
    nan_rows = 0
    for _, row in frame.iterrows():
        o = float(row["open"])
        h = float(row["high"])
        lo = float(row["low"])
        c = float(row["close"])
        if not (h >= o and h >= c and lo <= o and lo <= c and h >= lo and o > 0 and h > 0 and lo > 0 and c > 0):
            invalid_ohlc += 1
        if pd.notna(row["volume"]) and float(row["volume"]) < 0:
            negative_volume += 1
        if any(pd.isna(row[col]) for col in ("open", "high", "low", "close")):
            nan_rows += 1

    version = current["version"]
    lineage = {"manifest_exists": manifest is not None, "quality_report_exists": report is not None,
               "dataset_range_complete": requested_start is not None and requested_end is not None}
    if manifest:
        curated_artifact = lake._resolve_root_relative(str(manifest.get("curated_path", "")))
        quality_artifact = lake._resolve_root_relative(str(manifest.get("quality_report_path", "")))
        lineage["manifest_hashes_ok"] = bool(
            curated_artifact.exists() and quality_artifact.exists()
            and sha256_file(curated_artifact) == manifest.get("curated_sha256")
            and sha256_file(quality_artifact) == manifest.get("quality_report_sha256")
        )
        lineage["manifest_version"] = manifest.get("dataset_version")
        lineage["manifest_row_count"] = manifest.get("row_count")
        lineage["full_actual_start"] = manifest.get("full_actual_start")
        lineage["full_actual_end"] = manifest.get("full_actual_end")
        lineage["publication_run_id"] = manifest.get("publication_run_id")
        lineage["rule_version"] = manifest.get("cleaning_rule_version")
    if report:
        lineage["report_version"] = report.get("dataset_version") or report.get("run_id")
        lineage["report_actual_bars"] = report.get("actual_bars")
        lineage["report_complete_bars"] = report.get("complete_row_count")
        lineage["report_missing_intervals"] = report.get("missing_intervals", [])
        lineage["report_gap_provider"] = report.get("gap_provider")
        lineage["report_gap_unknown"] = report.get("gap_unknown")
        lineage["recomputed_gap_provider"] = breakdown["provider_gap"]
        lineage["recomputed_gap_unknown"] = breakdown["unknown"]
        lineage["report_quality_status"] = report.get("quality_status")
        lineage["report_backtest_suitable"] = report.get("backtest_suitable")

    with lake._connect() as db:
        catalog = db.execute(
            "SELECT run_id, status, version, row_count, start_time, end_time, asset_class, checksum "
            "FROM datasets WHERE symbol=? AND timeframe=? AND version=? ORDER BY created_at DESC LIMIT 1",
            (symbol.upper(), timeframe.upper(), version),
        ).fetchone()
    lineage["catalog_row"] = None if catalog is None else {
        "run_id": catalog[0], "status": catalog[1], "version": catalog[2],
        "row_count": catalog[3], "start_time": catalog[4], "end_time": catalog[5],
        "asset_class": catalog[6], "checksum": catalog[7],
    }

    if manifest is None or requested_start is None or requested_end is None:
        # Fail closed: without requested range or manifest the audit cannot
        # reconstruct the expected window and must not pretend 100%.
        return {
            "instrument_id": str(frame["instrument_id"].iloc[0]) if "instrument_id" in frame else "",
            "symbol": symbol.upper(),
            "asset_class": str(frame["asset_class"].iloc[0]) if "asset_class" in frame else "",
            "market": market,
            "venue": instrument.venue if instrument else "",
            "provider": str(frame["data_source"].iloc[0]) if "data_source" in frame else "",
            "frequency": timeframe.upper(),
            "timezone": "UTC",
            "status": "UNKNOWN",
            "requested_start": requested_start,
            "requested_end": requested_end,
            "actual_start": actual_start.isoformat(),
            "actual_end": actual_end.isoformat(),
            "provider_available_start": provider_available_start,
            "listing_start": listing_start,
            "expected_start": None,
            "expected_end": None,
            "stored_rows": stored,
            "complete_rows": complete,
            "incomplete_rows": stored - complete,
            "expected_rows": None,
            "missing_rows": None,
            "gap_provider": None,
            "gap_unknown": None,
            "duplicate_rows": exact_duplicates,
            "conflicting_duplicate_rows": conflicting_duplicates,
            "invalid_ohlc_rows": invalid_ohlc,
            "negative_volume_rows": negative_volume,
            "nan_rows": nan_rows,
            "coverage_ratio": None,
            "dataset_version": version,
            "quality_status": "UNKNOWN",
            "backtest_suitable": False,
            "latest_stored_bar": actual_end.isoformat(),
            "latest_complete_bar": complete_view["timestamp"].max().isoformat() if len(complete_view) else None,
            "lineage": lineage,
        }

    quality_status = (report.get("quality_status") if report else None) or "UNKNOWN"
    backtest_suitable = bool(report.get("backtest_suitable")) if report else False
    unexplained_missing = breakdown["provider_gap"] + breakdown["unknown"]
    if (
        timeframe.upper() == "D1"
        and instrument is not None
        and instrument.asset_class == "equity"
        and unexplained_missing > 0
    ):
        # Audit is an independent fail-closed check.  A stale or inconsistent
        # report cannot make a gapped formal equity dataset look suitable.
        quality_status = "QUARANTINED"
        backtest_suitable = False

    return {
        "instrument_id": str(frame["instrument_id"].iloc[0]) if "instrument_id" in frame else "",
        "symbol": symbol.upper(),
        "asset_class": str(frame["asset_class"].iloc[0]) if "asset_class" in frame else "",
        "market": market,
        "venue": instrument.venue if instrument else "",
        "provider": str(frame["data_source"].iloc[0]) if "data_source" in frame else "",
        "frequency": timeframe.upper(),
        "timezone": "UTC",
        "status": "OK" if quality_status == "CURATED" else quality_status,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "actual_start": actual_start.isoformat(),
        "actual_end": actual_end.isoformat(),
        "provider_available_start": provider_available_start,
        "listing_start": listing_start,
        "expected_start": expected_start.isoformat() if expected_start is not None else None,
        "expected_end": expected_end.isoformat() if expected_end is not None else None,
        "stored_rows": stored,
        "complete_rows": complete,
        "incomplete_rows": stored - complete,
        "expected_rows": theoretical,
        "missing_rows": missing,
        "gap_provider": breakdown["provider_gap"],
        "gap_unknown": breakdown["unknown"],
        "duplicate_rows": exact_duplicates,
        "conflicting_duplicate_rows": conflicting_duplicates,
        "invalid_ohlc_rows": invalid_ohlc,
        "negative_volume_rows": negative_volume,
        "nan_rows": nan_rows,
        "coverage_ratio": round(min(1.0, max(0.0, complete / theoretical)), 6) if theoretical else 0.0,
        "dataset_version": version,
        "quality_status": quality_status,
        "backtest_suitable": backtest_suitable,
        "latest_stored_bar": actual_end.isoformat(),
        "latest_complete_bar": complete_view["timestamp"].max().isoformat() if len(complete_view) else None,
        "lineage": lineage,
    }


def audit_universe(root: str | Path, symbols: list[str], timeframe: str = "D1") -> dict[str, object]:
    rows = [audit_dataset(root, symbol, timeframe) for symbol in symbols]
    curated = [r for r in rows if r.get("quality_status") == "CURATED"]
    return {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "timeframe": timeframe.upper(),
        "total": len(rows),
        "curated": len(curated),
        "rows": rows,
    }
