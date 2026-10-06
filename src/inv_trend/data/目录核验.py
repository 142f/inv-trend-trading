"""Read-only catalog/artifact auditing, independent of ingestion orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .integrity import sha256_file


def verify_catalog(lake) -> dict[str, object]:
    """Audit every catalog row against its manifest and every declared artifact.

    Reports valid records plus separately missing raw / normalized /
    quality / curated artifacts, hash mismatches, dangling or
    legacy-contaminated current pointers, and pointer-vs-catalog drift.
    A missing file is never hidden behind a "0 hash mismatch" number.
    """
    result = {
        "valid_dataset_records": 0,
        "missing_manifest_records": [],
        "missing_raw_records": [],
        "missing_normalized_records": [],
        "missing_quality_report_records": [],
        "missing_curated_records": [],
        "missing_dataset_manifest_records": [],
        "dangling_current_records": [],
        "hash_mismatch_records": [],
        "legacy_records": 0,
        "total_records": 0,
        "legacy_contaminated_current": [],
        "pointer_catalog_mismatch": [],
        "valid_current_records": 0,
    }
    with lake._connect() as db:
        rows = db.execute(
            "SELECT run_id, symbol, timeframe, status, version, manifest_path, checksum "
            "FROM datasets ORDER BY symbol, timeframe"
        ).fetchall()
    for run_id, symbol, timeframe, status, version, manifest_path, checksum in rows:
        result["total_records"] += 1
        if status == "LEGACY_ONLY":
            result["legacy_records"] += 1
            continue
        manifest_file = lake.root / "manifests" / f"{run_id}.json"
        if not manifest_file.exists():
            result["missing_manifest_records"].append(
                {"run_id": run_id, "symbol": symbol, "timeframe": timeframe}
            )
            continue
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        bucket = {"raw": [], "normalized": [], "quality": [], "curated": []}
        for raw_path in manifest.get("file_paths", []):
            candidate = resolve_lake_path(lake, raw_path)
            portable = raw_path.replace("\\", "/")
            if portable.startswith("raw/") or "/raw/" in portable:
                bucket["raw"].append((raw_path, candidate))
            elif portable.startswith("normalized/") or "/normalized/" in portable:
                bucket["normalized"].append((raw_path, candidate))
            elif portable.startswith("ingestion_reports/"):
                bucket["quality"].append((raw_path, candidate))
            elif (portable.startswith("curated/") or "/curated/" in portable) and raw_path.endswith(
                "bars.parquet"
            ):
                bucket["curated"].append((raw_path, candidate))
        for kind, key in (
            ("raw", "missing_raw_records"),
            ("normalized", "missing_normalized_records"),
            ("quality", "missing_quality_report_records"),
            ("curated", "missing_curated_records"),
        ):
            missing = [p for p, resolved in bucket[kind] if not resolved.exists()]
            if missing and kind == "curated" and status in {"QUARANTINED", "REVIEW_REQUIRED"}:
                continue  # quarantined/review rows legitimately have no curated artifact
            if missing:
                result[key].append(
                    {
                        "run_id": run_id,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "missing_files": missing,
                        "version": version,
                    }
                )
        bad = []
        for raw_path in manifest.get("file_paths", []):
            candidate = resolve_lake_path(lake, raw_path)
            if candidate.exists() and sha256_file(candidate) != manifest["file_hashes"].get(
                raw_path
            ):
                bad.append(raw_path)
        if bad:
            result["hash_mismatch_records"].append(
                {"run_id": run_id, "symbol": symbol, "files": bad}
            )
            continue
        # A record is valid only when every declared artifact exists and hashes match.
        all_declared_exist = all(
            resolve_lake_path(lake, p).exists() for p in manifest.get("file_paths", [])
        )
        if all_declared_exist:
            result["valid_dataset_records"] += 1
    for pointer in (lake.root / "curated").glob("symbol=*/timeframe=*/*.json"):
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        raw_target = str(payload.get("path", ""))
        target = resolve_lake_path(lake, raw_target)
        if not target.exists():
            result["dangling_current_records"].append(
                {
                    "pointer": str(pointer),
                    "version": payload.get("version"),
                    "expected_path": raw_target,
                }
            )
        elif "/asset_class=legacy/" in raw_target.replace("\\", "/"):
            result["legacy_contaminated_current"].append(
                {
                    "pointer": str(pointer),
                    "version": payload.get("version"),
                    "path": raw_target,
                }
            )
    # Pointer vs catalog drift for the formal channel.
    with lake._connect() as db:
        catalog_current = {
            (row[0].upper(), row[1].upper()): (row[2], row[3])
            for row in db.execute(
                "SELECT symbol, timeframe, version, run_id FROM current_versions"
            ).fetchall()
        }
    for symbol_dir in (lake.root / "curated").glob("symbol=*"):
        symbol = symbol_dir.name.split("=", 1)[1].upper()
        for timeframe_dir in symbol_dir.glob("timeframe=*"):
            timeframe = timeframe_dir.name.split("=", 1)[1].upper()
            pointer = lake.current_version(symbol, timeframe)
            catalog_entry = catalog_current.get((symbol, timeframe))
            catalog_version = catalog_entry[0] if catalog_entry else None
            catalog_run_id = catalog_entry[1] if catalog_entry else None
            if pointer is not None and (
                pointer.get("version") != catalog_version or pointer.get("run_id") != catalog_run_id
            ):
                result["pointer_catalog_mismatch"].append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "pointer_version": pointer.get("version"),
                        "catalog_version": catalog_version,
                        "pointer_run_id": pointer.get("run_id"),
                        "catalog_run_id": catalog_run_id,
                    }
                )
            if pointer is None:
                continue
            dataset_manifest_ref = pointer.get("dataset_manifest_path")
            dataset_manifest_path = (
                resolve_lake_path(lake, str(dataset_manifest_ref))
                if dataset_manifest_ref
                else lake.root / "dataset_manifests" / f"{pointer.get('version')}.json"
            )
            if not dataset_manifest_path.exists():
                result["missing_dataset_manifest_records"].append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "version": pointer.get("version"),
                        "path": str(dataset_manifest_path),
                    }
                )
                continue
            dataset_manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
            curated = resolve_lake_path(lake, str(dataset_manifest.get("curated_path", "")))
            quality = resolve_lake_path(lake, str(dataset_manifest.get("quality_report_path", "")))
            dataset_ok = bool(
                dataset_manifest.get("dataset_version") == pointer.get("version")
                and dataset_manifest.get("publication_run_id") == pointer.get("run_id")
                and curated.exists()
                and quality.exists()
                and sha256_file(curated) == dataset_manifest.get("curated_sha256")
                and sha256_file(quality) == dataset_manifest.get("quality_report_sha256")
            )
            if dataset_ok:
                report = json.loads(quality.read_text(encoding="utf-8"))
                frame = pd.read_parquet(curated, columns=["dataset_version"])
                dataset_ok = bool(
                    report.get("dataset_version") == pointer.get("version")
                    and not frame.empty
                    and set(frame["dataset_version"].astype(str)) == {str(pointer.get("version"))}
                    and len(frame) == int(dataset_manifest.get("row_count", -1))
                )
            if dataset_ok and pointer.get("version") == catalog_version:
                result["valid_current_records"] += 1
            elif not dataset_ok:
                result["hash_mismatch_records"].append(
                    {
                        "run_id": pointer.get("run_id"),
                        "symbol": symbol,
                        "files": [str(dataset_manifest_path)],
                        "kind": "dataset_lineage",
                    }
                )
    return result


def resolve_lake_path(lake, raw_path: str) -> Path:
    """Resolve a manifest/pointer path against the data root (portable paths).

    Accepts root-relative paths (``curated/...``), project-relative legacy
    paths (``data\\curated\\...``) and absolute paths for backwards compat.
    """
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    root_relative = lake.root / candidate
    if root_relative.exists():
        return root_relative
    if candidate.exists():
        return candidate
    return root_relative
