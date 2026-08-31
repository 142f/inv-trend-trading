"""Unified strategy backtest, comparison, and report CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

import yaml

from inv_trend.application.backtest import (
    BacktestArtifactWriter,
    BacktestPlan,
    BacktestBatchService,
    expand_parameter_grid,
    load_source_bundle,
    load_versioned_data,
    signal_parameters,
)
from inv_trend.application.backtest.models import canonical_hash


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-plan", help="Validate and expand a plan only")
    validate.add_argument("--plan", required=True)
    validate.add_argument("--source-run")
    run = subparsers.add_parser("run", help="Execute an immutable backtest batch")
    run.add_argument("--source-run", required=True)
    run.add_argument("--plan", required=True)
    run.add_argument("--data-root", default="data")
    run.add_argument("--output-dir", default="outputs/backtest")
    run.add_argument("--run-id", type=_run_id)
    report = subparsers.add_parser(
        "report", help="Regenerate HTML from an existing v1/v2 batch result"
    )
    report.add_argument("--result", required=True)
    report.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None and Path(sys.argv[0]).stem == "turtle-backtest":
        print(
            "warning: turtle-backtest is deprecated; use strategy-backtest",
            file=sys.stderr,
        )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            target = BacktestArtifactWriter().regenerate_report(args.result, args.output)
            print(json.dumps({
                "stage": "strategy-backtest-report", "status": "COMPLETED",
                "report": str(target.resolve()),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        plan = load_plan(args.plan)
        combinations = expand_parameter_grid(plan)
        if args.command == "validate-plan":
            source_hash = None
            if args.source_run:
                source_hash = load_source_bundle(args.source_run, plan).source_hash
            groups = {canonical_hash(signal_parameters(item)) for item in combinations}
            print(json.dumps({
                "status": "VALID", "schema_version": plan.schema_version,
                "combination_count": len(combinations),
                "signal_parameter_group_count": len(groups),
                "source_hash": source_hash,
                "combinations": combinations,
            }, ensure_ascii=False, sort_keys=True, default=str))
            return 0
        result = run_backtest_stage(
            source_run=args.source_run,
            plan=plan,
            data_root=args.data_root,
            output_dir=args.output_dir,
            run_id=args.run_id,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    except KeyboardInterrupt:
        print("strategy backtest interrupted by user", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"strategy backtest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def load_plan(path: str | Path) -> BacktestPlan:
    plan_path = Path(path)
    if not plan_path.is_file():
        raise FileNotFoundError(f"backtest plan is missing: {plan_path}")
    raw = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("backtest plan must contain a YAML mapping")
    if raw.get("strategy_config_path"):
        configured = Path(str(raw["strategy_config_path"]))
        if not configured.is_absolute():
            raw["strategy_config_path"] = str((plan_path.parent / configured).resolve())
    return BacktestPlan.from_mapping(raw)


def run_backtest_stage(
    *,
    source_run: str | Path,
    plan: BacktestPlan,
    data_root: str | Path = "data",
    output_dir: str | Path = "outputs/backtest",
    run_id: str | None = None,
) -> dict[str, Any]:
    source = load_source_bundle(source_run, plan)
    data, lineage = load_versioned_data(source, data_root)
    batch = BacktestBatchService().run(source, plan, data, run_id=run_id)
    artifacts = BacktestArtifactWriter(output_dir).write(batch, lineage=lineage)
    return {
        "stage": "strategy-backtest", "status": "COMPLETED",
        "run_id": batch.run_id, "result_hash": batch.result_hash,
        "validation_status": batch.validation_status,
        "best_combination_id": batch.best_combination_id,
        "stable_combination_ids": list(batch.stable_combination_ids),
        "artifacts": artifacts,
    }


def _run_id(value: str) -> str:
    if not _SAFE_RUN_ID.fullmatch(value) or value in {".", ".."}:
        raise argparse.ArgumentTypeError("run ID must be a safe ASCII path segment")
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "load_plan", "main", "run_backtest_stage"]
