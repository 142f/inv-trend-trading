"""Immutable Chinese-named artifacts for unified backtest batches."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

import pandas as pd

from .models import BacktestBatchResult
from .reporting import build_report_model, render_backtest_html


class BacktestArtifactWriter:
    def __init__(self, output_root: str | Path = "outputs/backtest") -> None:
        self.output_root = Path(output_root)

    def write(
        self,
        batch: BacktestBatchResult,
        *,
        lineage: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, str]:
        run_root = self.output_root / "runs" / batch.report_date / batch.run_id
        if run_root.exists():
            raise FileExistsError(f"immutable backtest run already exists: {run_root}")
        inputs = run_root / "01_输入"
        combinations_root = run_root / "02_参数组合"
        exports = run_root / "03_汇总导出"
        reports = run_root / "04_可视化报告"
        audit = run_root / "05_审计"
        for directory in (inputs, combinations_root, exports, reports, audit):
            directory.mkdir(parents=True, exist_ok=False)
        prefix = _prefix(batch)
        _write_json(inputs / "回测输入清单_v2.json", {
            "source": batch.source.to_dict(), "plan": batch.plan.to_dict(),
            "lineage": lineage,
        })
        _write_json(inputs / "参数组合展开_v2.json", {
            "combination_count": len(batch.combinations),
            "combinations": [
                {"combination_id": item.combination_id, "parameters": item.parameters}
                for item in batch.combinations
            ],
        })
        for bundle in batch.signal_bundles:
            _write_json(
                inputs / f"信号投影_{bundle.signal_parameter_hash[:12]}_v2.json",
                bundle.to_dict(),
            )
        summary_rows: list[dict[str, Any]] = []
        all_trades: list[dict[str, Any]] = []
        for item in batch.combinations:
            directory = combinations_root / item.combination_id
            directory.mkdir()
            _write_json(directory / "组合结果_v2.json", item.to_dict())
            pd.DataFrame(item.equity_curve).to_csv(
                directory / "权益曲线_v2.csv", index=False, encoding="utf-8-sig"
            )
            pd.DataFrame(item.drawdown_curve).to_csv(
                directory / "回撤曲线_v2.csv", index=False, encoding="utf-8-sig"
            )
            pd.DataFrame(item.trades).to_parquet(
                directory / "交易明细_v2.parquet", index=False
            )
            pd.DataFrame(item.orders).to_csv(
                directory / "订单明细_v2.csv", index=False, encoding="utf-8-sig"
            )
            summary_rows.append(_summary_row(item))
            all_trades.extend(
                {"combination_id": item.combination_id, **dict(row)}
                for row in item.trades
            )
        metrics_path = exports / f"{prefix}_参数组合指标_v2.csv"
        trades_path = exports / f"{prefix}_交易明细_v2.parquet"
        result_path = exports / f"{prefix}_批次结果_v2.json"
        pd.DataFrame(summary_rows).to_csv(metrics_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(all_trades).to_parquet(trades_path, index=False)
        _write_json(result_path, batch.to_dict())
        report_model = build_report_model(batch)
        model_path = reports / f"{prefix}_报告数据模型_v1.json"
        report_path = reports / f"{prefix}_参数比较报告_v2.html"
        _write_json(model_path, report_model.to_dict())
        report_path.write_text(render_backtest_html(report_model), encoding="utf-8")
        manifest = {
            "schema_version": "2", "run_id": batch.run_id,
            "report_date": batch.report_date, "result_hash": batch.result_hash,
            "source_hash": batch.source.source_hash,
            "comparison_assumptions_hash": batch.comparison_assumptions_hash,
            "strategy_id": batch.source.strategy_id,
            "strategy_version": batch.source.strategy_version,
            "code_version": batch.source.code_version,
            "data_versions": {
                item.symbol: item.dataset_version for item in batch.source.instruments
            },
            "generated_at": batch.generated_at,
            "execution_result_hashes": {
                item.combination_id: item.execution_result_hash
                for item in batch.combinations
            },
        }
        _write_json(audit / "可复现运行清单_v2.json", manifest)
        _write_json(audit / "清理与兼容审计_v2.json", {
            "schema_version": "2",
            "canonical_module": "inv_trend.application.backtest",
            "canonical_cli": "strategy-backtest",
            "compatibility_period": "one release",
            "compatibility_entries": [
                "turtle-backtest", "BacktestService", "StrategyBacktestService",
                "inv_trend.application.strategy_backtest", "DetectorBacktester",
            ],
            "legacy_outputs": "preserved read-only",
            "browser_validation": {
                "status": "UNAVAILABLE_IN_ENVIRONMENT",
                "automated_contract_validation_required": True,
            },
        })
        index = {
            **manifest,
            "validation_status": batch.validation_status,
            "best_combination_id": batch.best_combination_id,
            "stable_combination_ids": list(batch.stable_combination_ids),
            "combination_count": len(batch.combinations),
            "conclusion": batch.conclusion,
            "report": _relative(report_path, run_root),
            "report_model": _relative(model_path, run_root),
            "metrics": _relative(metrics_path, run_root),
            "batch_result": _relative(result_path, run_root),
        }
        index_path = run_root / "回测批次索引_v2.json"
        _write_json(index_path, index)
        hashes = _artifact_hashes(run_root, exclude={"文件哈希_v2.json"})
        _write_json(audit / "文件哈希_v2.json", hashes)
        self._write_archive_index()
        self._update_latest(run_root, report_path, index_path)
        return {
            "run_directory": str(run_root.resolve()),
            "report": str(report_path.resolve()),
            "report_model": str(model_path.resolve()),
            "index": str(index_path.resolve()),
            "metrics": str(metrics_path.resolve()),
            "trades": str(trades_path.resolve()),
            "batch_result": str(result_path.resolve()),
        }

    def regenerate_report(
        self,
        batch_result: str | Path,
        output: str | Path,
    ) -> Path:
        """Regenerate v1/v2 HTML from stored JSON without running a strategy."""

        raw = json.loads(Path(batch_result).read_text(encoding="utf-8"))
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_backtest_html(raw), encoding="utf-8")
        return target

    def _write_archive_index(self) -> None:
        parent = self.output_root.parent
        candidates = (
            parent / "strategy_backtest",
            parent / "backtest_corrected_v2",
            parent / "backtest_legacy_v1",
            parent / "d1_standard_backtest",
        )
        archives = [
            {
                "path": str(path.resolve()),
                "format": "历史格式，只读保留",
                "exists": path.exists(),
            }
            for path in candidates if path.resolve() != self.output_root.resolve()
        ]
        _write_json(self.output_root / "历史回测归档索引_v1.json", {
            "schema_version": "1", "policy": "只读保留，不迁移、不覆盖",
            "archives": archives,
        })

    def _update_latest(self, run_root: Path, report: Path, index: Path) -> None:
        latest = self.output_root / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        _atomic_copy(report, latest / "最新参数比较报告_v2.html")
        _atomic_copy(index, latest / "最新回测批次索引_v2.json")
        _write_json(self.output_root / "latest.json", {
            "schema_version": "2",
            "run_directory": str(run_root.resolve()),
            "report": str(report.resolve()),
            "index": str(index.resolve()),
        })


def _summary_row(item: Any) -> dict[str, Any]:
    return {
        "rank": item.rank, "combination_id": item.combination_id,
        "status": item.status, "qualified": item.qualified, "score": item.score,
        "stability_score": item.stability_score,
        "execution_result_hash": item.execution_result_hash,
        **{f"full_{key}": value for key, value in item.metrics.items()},
        **{f"oos_{key}": value for key, value in item.validation_metrics.items()},
        **{f"holdout_{key}": value for key, value in item.holdout_metrics.items()},
        "parameters": json.dumps(item.parameters, ensure_ascii=False, sort_keys=True),
        "overfit_risk": item.overfit_risk.get("level"),
    }


def _prefix(batch: BacktestBatchResult) -> str:
    strategy = "海龟" if batch.source.strategy_id == "turtle" else batch.source.strategy_id
    symbols = "_".join(item.symbol for item in batch.source.instruments)
    timeframes = "_".join(sorted({item.timeframe for item in batch.source.instruments}))
    date = batch.report_date.replace("-", "")
    raw = f"{strategy}_{symbols}_{timeframes}_{date}_{batch.run_id}"
    return re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", raw).strip("_")


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, indent=2, allow_nan=False, default=str
    ) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def _artifact_hashes(root: Path, exclude: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and item.name not in exclude
    ):
        values[_relative(path, root)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


__all__ = ["BacktestArtifactWriter", "render_backtest_html"]
