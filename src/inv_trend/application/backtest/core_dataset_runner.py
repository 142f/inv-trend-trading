"""Core-dataset preparation plus its backtest evaluation (application use case).

Data preparation itself stays in ``inv_trend.adapters.multi_asset.data``; this
module owns the orchestration that runs the prepared dataset through the unified
executor, reconciles it against the merged frame, and exports the
baseline/rebuilt report set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from inv_trend.adapters.multi_asset.config import BacktestConfig
from inv_trend.adapters.multi_asset.data.core_dataset import (
    CORE_DATASET_NAME,
    build_asset_specs,
    build_contribution_table,
    build_daily_equity_curve,
    build_signal_table,
    compare_summaries,
    prepare_metal_tech_core_dataset,
    summarize_backtest_result,
)
from inv_trend.adapters.multi_asset.data.exporter import export_csv, export_json
from inv_trend.adapters.multi_asset.data.loader import load_backtest_ready_csv
from inv_trend.adapters.multi_asset.strategy.profiles import turtle_rules

from .execution import UnifiedBacktestExecutor
from .single_artifacts import write_single_backtest_outputs


def build_metal_tech_core_dataset(
    processed_dir: str | Path = "processed_data",
    output_dir: str | Path = "processed_data",
    reports_dir: str | Path = "outputs",
    backtest_config: BacktestConfig | None = None,
) -> dict[str, object]:
    """Prepare the core dataset, evaluate it, and return the legacy summary."""

    prepared = prepare_metal_tech_core_dataset(
        processed_dir=processed_dir, output_dir=output_dir
    )
    report_payload = run_core_backtest_and_compare(
        backtest_path=prepared.backtest_ready_path,
        selected_frames=prepared.selected_frames,
        reports_dir=reports_dir,
        backtest_config=backtest_config,
    )

    report_json_path = Path(reports_dir) / CORE_DATASET_NAME / "report.json"
    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.write_text(
        json.dumps(report_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    return {**prepared.summary, "report_path": str(report_json_path)}


def run_core_backtest_and_compare(
    *,
    backtest_path: Path,
    selected_frames: dict[str, pd.DataFrame],
    reports_dir: str | Path,
    backtest_config: BacktestConfig | None = None,
) -> dict[str, object]:
    reports_root = Path(reports_dir) / CORE_DATASET_NAME
    reports_root.mkdir(parents=True, exist_ok=True)

    original_data = {
        symbol: frame.sort_values("date").set_index("date")[["open", "high", "low", "close", "volume", "spread"]]
        for symbol, frame in selected_frames.items()
    }
    merged_data = load_backtest_ready_csv(backtest_path)
    specs = build_asset_specs(sorted(selected_frames))
    rules = turtle_rules("classic-bars")

    baseline_result = UnifiedBacktestExecutor(
        original_data,
        specs,
        rules,
        config=backtest_config,
    ).run()
    rebuilt_result = UnifiedBacktestExecutor(
        merged_data,
        specs,
        rules,
        config=backtest_config,
    ).run()

    baseline_summary = summarize_backtest_result(baseline_result)
    rebuilt_summary = summarize_backtest_result(rebuilt_result)
    comparison = compare_summaries(baseline_summary, rebuilt_summary)

    export_backtest_outputs(
        baseline_result,
        reports_root / "baseline",
        strategy_version=backtest_config.strategy_version,
        html=backtest_config.html_report,
    )
    export_backtest_outputs(
        rebuilt_result,
        reports_root / "rebuilt",
        strategy_version=backtest_config.strategy_version,
        html=backtest_config.html_report,
    )
    export_csv(build_signal_table(rebuilt_result.orders), reports_root / "buy_sell_signals.csv")
    export_csv(build_contribution_table(rebuilt_result.trades), reports_root / "symbol_contribution.csv")
    export_csv(build_daily_equity_curve(rebuilt_result.equity_curve), reports_root / "daily_equity_curve.csv")
    export_json(baseline_summary, reports_root / "baseline_metrics.json")
    export_json(rebuilt_summary, reports_root / "rebuilt_metrics.json")
    export_json(comparison, reports_root / "comparison.json")

    return {
        "baseline": baseline_summary,
        "rebuilt": rebuilt_summary,
        "comparison": comparison,
    }


def export_backtest_outputs(
    result: object, out_dir: Path, *, strategy_version: str = "corrected-v2",
    html: bool = True,
) -> None:
    write_single_backtest_outputs(
        result, out_dir, strategy_version=strategy_version, html=html
    )


__all__ = [
    "build_metal_tech_core_dataset",
    "export_backtest_outputs",
    "run_core_backtest_and_compare",
]
