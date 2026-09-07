"""v3 projections: one detail table per kind, small reconstruction metadata."""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

from .基础 import encoded, redact
from .仓库 import Storage, parquet_bytes, project_root


def backtest_publish(writer, batch, lineage):
    from inv_trend.application.backtest.reporting import build_report_model, render_backtest_html
    root = project_root(writer.output_root)
    storage = Storage(root)
    payload = batch.to_dict()
    files, detail_tables = {}, {}
    combinations = payload["combinations"]
    experiments = []
    for combination in combinations:
        cid = combination["combination_id"]
        metrics = []
        for field, role in (("metrics", "FULL"), ("validation_metrics", "VALIDATION"), ("holdout_metrics", "HOLDOUT")):
            for name, value in combination.get(field, {}).items():
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    metrics.append(dict(sample_role=role, metric_name=name, value=value))
        # Preserve arbitrary fold/scenario results in metadata even when they
        # are not a scalar; scalar nested metrics have independent dimensions.
        for i, fold in enumerate(combination.get("folds", [])):
            for field, values in fold.items():
                if isinstance(values, dict):
                    for name, value in values.items():
                        if isinstance(value, (float, int)) and not isinstance(value, bool):
                            metrics.append(dict(sample_role=field, window_id=str(i), fold_id=str(i), metric_name=name, value=value, scenario={"field": field}))
        experiments.append(dict(id=cid, parameters=combination["parameters"], metrics=metrics,
                                result_hash=combination.get("execution_result_hash"), status=combination.get("status", "COMPLETED")))
        for key in ("trades", "orders", "equity_curve", "drawdown_curve"):
            rows = combination.pop(key, [])
            detail_tables.setdefault(key, []).extend(
                {"combination_id": cid, "row_number": i, "payload": encoded(row).decode()}
                for i, row in enumerate(rows))
            combination[key] = {"artifact": f"明细/{key}.parquet", "combination_id": cid}
    for key, rows in detail_tables.items():
        # payload preserves exact legacy scalar/list structure; useful top-level
        # fields are also exposed as typed columns for DuckDB filtering.
        frame_rows = []
        for row in rows:
            value = json.loads(row["payload"])
            flat = {k: v for k, v in value.items() if isinstance(v, (str, int, float, bool)) or v is None} if isinstance(value, dict) else {}
            frame_rows.append({**flat, **row})
        frame = pd.DataFrame(frame_rows) if frame_rows else pd.DataFrame(columns=["combination_id", "row_number", "payload"])
        files[f"明细/{key}.parquet"] = parquet_bytes(frame)
    payload["storage_version"] = 3
    files["输入/批次结果_v3.json"] = encoded(redact(payload))
    files["输入/数据血缘_v3.json"] = encoded(redact(lineage))
    # HTML is the one retained presentation, not a second report-model JSON.
    files["报告/参数比较报告.html"] = render_backtest_html(build_report_model(batch)).encode("utf-8")
    datasets = [dict(version=str(v["dataset_version"]), checksum=str(v["curated_sha256"]),
                     manifest_path=str(v["dataset_manifest"]), symbol=k)
                for k, v in lineage.items() if all(v.get(f) for f in ("dataset_version", "curated_sha256", "dataset_manifest"))]
    paths = storage.publish(writer.output_root.name, batch.run_id, batch.report_date, files,
                            facts={"experiments": experiments, "datasets": datasets}, config=batch.plan.to_dict())
    manifest = storage.rows("SELECT manifest_path FROM runs WHERE run_id=?", (batch.run_id,))[0]["manifest_path"]
    index = storage.root / manifest
    return {"run_directory": str(index.parent.parent), "report": str(paths["报告/参数比较报告.html"]),
            "report_model": str(paths["输入/批次结果_v3.json"]), "index": str(index),
            "metrics": str(storage.path), "trades": str(paths["明细/trades.parquet"]),
            "batch_result": str(paths["输入/批次结果_v3.json"])}


def read_backtest(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("storage_version") != 3:
        return raw
    # Locate the manifest through its indexed blob; never trust a latest view.
    root = project_root(path)
    storage = Storage(root)
    run_id = raw["run_id"]
    manifest = storage.manifest(run_id)
    storage._verify_manifest(manifest)
    paths = {f["logical_name"]: storage.root / f["path"] for f in manifest["files"]}
    tables = {}
    for combination in raw["combinations"]:
        for key in ("trades", "orders", "equity_curve", "drawdown_curve"):
            ref = combination[key]
            name = ref["artifact"]
            if name not in tables:
                tables[name] = pd.read_parquet(paths[name])
            frame = tables[name]
            selected = frame[frame.combination_id == ref["combination_id"]].sort_values("row_number")
            combination[key] = [json.loads(v) for v in selected.payload]
    raw.pop("storage_version", None)
    return raw


def daily_publish(writer, snapshot, render_html):
    from inv_trend.adapters.daily.artifact_publisher import _complete_result, _stage, _publication
    storage = Storage(project_root(writer.artifact_root))
    files, complete = {}, {}
    for row in snapshot.get("symbols", []):
        symbol = str(row.get("symbol", "UNKNOWN"))
        complete[symbol] = _complete_result(snapshot, row)
        complete[symbol]["storage_source_row"] = row
        # Stage views already occur in complete analysis; logical stage lookup
        # reconstructs them, instead of retaining four overlapping JSON files.
        files[f"输入/{symbol}/完整分析.json"] = encoded(redact(complete[symbol]))
    # The snapshot retains orchestration metadata; symbol payloads are references.
    compact = {**snapshot, "symbols": [{"symbol": s, "artifact": f"输入/{s}/完整分析.json"} for s in complete], "storage_version": 3}
    files["输入/日报快照_v3.json"] = encoded(redact(compact))
    if render_html:
        files["报告/趋势分析报告.html"] = writer._render_complete_analyses(complete).encode("utf-8")
    paths = storage.publish(writer.artifact_root.name, str(snapshot["run_id"]), str(snapshot["report_date"]), files,
                            config=snapshot.get("configuration", {}), facts={"render_html": bool(render_html)})
    manifest_path = storage.rows("SELECT manifest_path FROM runs WHERE run_id=?", (snapshot["run_id"],))[0]["manifest_path"]
    return _publication(run_directory=(storage.root / manifest_path).parent.parent,
                        compatibility_json=paths["输入/日报快照_v3.json"],
                        compatibility_html=paths.get("报告/趋势分析报告.html"),
                        complete_results={s: paths[f"输入/{s}/完整分析.json"] for s in complete})


def read_daily(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("storage_version") != 3:
        return raw
    storage = Storage(project_root(path))
    manifest = storage.manifest(raw["run_id"])
    storage._verify_manifest(manifest)
    paths = {f["logical_name"]: storage.root / f["path"] for f in manifest["files"]}
    # Complete analyses contain original stage records; reconstruct the snapshot
    # from each complete result's source row retained by the v3 daily adapter.
    raw["symbols"] = [json.loads(paths[item["artifact"]].read_text(encoding="utf-8"))["storage_source_row"] for item in raw["symbols"]]
    raw.pop("storage_version", None)
    return raw
