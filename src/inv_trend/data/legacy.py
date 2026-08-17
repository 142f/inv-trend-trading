from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from .models import InstrumentConfig
from .processing import normalize_bars
from .storage import DataLake


def migrate_legacy_csv(source_dir: str | Path, root: str | Path = "data") -> dict[str, object]:
    """Import legacy processed CSV files without claiming unavailable raw lineage."""
    source = Path(source_dir)
    lake = DataLake(root)
    candidates = sorted((source / "cleaned").rglob("*.csv")) if (source / "cleaned").exists() else sorted(source.rglob("*.csv"))
    selected: dict[tuple[str, str], tuple[Path, pd.DataFrame]] = {}
    for path in candidates:
        frame = pd.read_csv(path)
        if frame.empty or "symbol" not in frame or not {"open", "high", "low", "close"}.issubset(frame):
            continue
        symbol = str(frame["symbol"].iloc[0]).upper()
        timeframe = str(frame.get("timeframe", pd.Series(["D1"])).iloc[0]).upper()
        key = (symbol, timeframe)
        if key not in selected or len(frame) > len(selected[key][1]):
            selected[key] = (path, frame)
    migrated: list[dict[str, object]] = []
    for (symbol, timeframe), (path, raw) in selected.items():
        config = InstrumentConfig(
            symbol=symbol, source_symbol=symbol, instrument_id=f"{symbol}.LEGACY.CSV",
            asset_class="legacy", market="legacy", instrument_type="spot", quote_currency="USD",
            timezone="UTC", session="24x7", primary_source="legacy_csv", earliest_valid_date=None,
        )
        normalized = normalize_bars(raw, config, timeframe, "LEGACY_CSV_MIGRATION")
        clean = normalized.clean.copy()
        run_id = hashlib.sha256(str(path.resolve()).encode() + pd.util.hash_pandas_object(raw, index=True).values.tobytes()).hexdigest()[:24]
        version = f"legacy-{run_id}"
        manifest_path = lake.root / "manifests" / f"legacy-{run_id}.json"
        if manifest_path.exists():
            migrated.append({"symbol": symbol, "timeframe": timeframe, "path": str(path), "run_id": run_id,
                             "status": "already_migrated"})
            continue
        clean["quality_status"] = "LEGACY_ONLY"
        clean["quality_score"] = 10.0
        clean["quality_flags"] = "RAW_SOURCE_UNAVAILABLE;PROCESSING_HISTORY_UNCLEAR;NO_PROVIDER_VERIFICATION"
        clean["raw_file_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
        clean["request_id"] = run_id
        clean["raw_snapshot_id"] = clean["raw_file_hash"]
        clean["source_run_id"] = run_id
        clean["dataset_version"] = version
        lake.write_normalized(clean, "legacy", config.instrument_id, timeframe)
        # Legacy is a distinct publication channel and can never move the
        # strategy-visible curated current pointer.
        lake.publish_curated(clean, symbol=symbol, instrument_id=config.instrument_id, asset_class="legacy",
                             timeframe=timeframe, version=version, run_id=run_id, channel="legacy")
        manifest = lake.write_json({
            "run_id": run_id, "symbol": symbol, "instrument_id": config.instrument_id,
            "timeframe": timeframe, "quality_status": "LEGACY_ONLY", "quality_score": 10.0,
            "source_path": str(path), "source_file_hash": clean["raw_file_hash"].iloc[0],
            "raw_source_available": False,
        }, Path("manifests") / f"legacy-{run_id}.json")
        lake.catalog(run_id, symbol, timeframe, "LEGACY_CSV_MIGRATION", manifest,
                     instrument_id=config.instrument_id, status="LEGACY_ONLY", version=version)
        migrated.append({"symbol": symbol, "timeframe": timeframe, "path": str(path), "run_id": run_id,
                         "status": "migrated"})
    return {"source": str(source), "migrated": migrated, "count": len(migrated)}
