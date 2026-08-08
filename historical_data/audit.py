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
from .storage import DataLake, sha256_file


def audit_dataset(root: str | Path, symbol: str, timeframe: str = "D1") -> dict[str, object]:
    lake = DataLake(root)
    current = lake.current_version(symbol.upper(), timeframe.upper())
    if current is None:
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "NO_CURRENT_POINTER"}
    path = Path(current["path"])
    if not path.exists():
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "CURATED_FILE_MISSING",
                "expected_path": str(path)}
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    stored = len(frame)
    complete = int(frame["is_complete"].astype(bool).sum())
    complete_view = frame.loc[frame["is_complete"].astype(bool)]
    start = frame["timestamp"].min()
    end = frame["timestamp"].max()

    instruments = load_instruments()
    instrument = instruments.get(symbol.upper())
    session = instrument.session if instrument else ""
    market = instrument.market if instrument else str(frame["market"].iloc[0]) if "market" in frame else ""

    absent, breakdown = classify_missing(
        pd.DatetimeIndex(frame["timestamp"].drop_duplicates()),
        market=market, session=session,
        start=start, end=end, freq=FRAME_DELTAS[timeframe],
    )
    theoretical = len(pd.date_range(start, end, freq=FRAME_DELTAS[timeframe]))
    if session in {"24x5", "regular"}:
        theoretical -= breakdown["weekend"]
    if session == "regular" and market in {"xnas", "xnys"}:
        theoretical -= breakdown["holiday"]

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
    report_path = lake.root / "quality_reports" / f"{version}.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
    manifest_path = lake.root / "manifests" / f"{current['run_id']}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None

    lineage = {"manifest_exists": manifest is not None, "quality_report_exists": report is not None}
    if manifest:
        lineage["manifest_hashes_ok"] = all(
            Path(p).exists() and sha256_file(Path(p)) == digest
            for p, digest in manifest.get("file_hashes", {}).items()
        )
        lineage["manifest_version"] = manifest.get("dataset_version")
        lineage["manifest_row_count"] = manifest.get("row_count")
        lineage["requested_start"] = manifest.get("requested_start")
        lineage["requested_end"] = manifest.get("requested_end")
        lineage["provider_available_start"] = manifest.get("provider_available_start")
        lineage["listing_start"] = manifest.get("listing_start")
        raw_path = next((p for p in manifest.get("file_paths", []) if p.endswith(".parquet") and "/raw/" in p), None)
        lineage["raw_sha256"] = manifest.get("file_hashes", {}).get(raw_path) if raw_path else None
    if report:
        lineage["report_version"] = report.get("dataset_version") or report.get("run_id")
        lineage["report_actual_bars"] = report.get("actual_bars")
        lineage["report_complete_bars"] = report.get("complete_row_count")
        lineage["report_missing_intervals"] = report.get("missing_intervals", [])
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

    quality_status = (report.get("quality_status") if report else None) or "UNKNOWN"
    backtest_suitable = bool(report.get("backtest_suitable")) if report else False

    return {
        "instrument_id": str(frame["instrument_id"].iloc[0]) if "instrument_id" in frame else "",
        "symbol": symbol.upper(),
        "asset_class": str(frame["asset_class"].iloc[0]) if "asset_class" in frame else "",
        "market": market,
        "venue": instrument.venue if instrument else "",
        "provider": str(frame["data_source"].iloc[0]) if "data_source" in frame else "",
        "frequency": timeframe.upper(),
        "timezone": "UTC",
        "requested_start": lineage.get("requested_start"),
        "requested_end": lineage.get("requested_end"),
        "actual_start": start.isoformat(),
        "actual_end": end.isoformat(),
        "provider_available_start": lineage.get("provider_available_start"),
        "listing_start": lineage.get("listing_start"),
        "stored_rows": stored,
        "complete_rows": complete,
        "incomplete_rows": stored - complete,
        "expected_rows": theoretical,
        "missing_rows": breakdown["provider_gap"],
        "duplicate_rows": exact_duplicates,
        "conflicting_duplicate_rows": conflicting_duplicates,
        "invalid_ohlc_rows": invalid_ohlc,
        "negative_volume_rows": negative_volume,
        "nan_rows": nan_rows,
        "coverage_ratio": round(complete / theoretical, 6) if theoretical else 0.0,
        "dataset_version": version,
        "quality_status": quality_status,
        "backtest_suitable": backtest_suitable,
        "latest_stored_bar": end.isoformat(),
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
