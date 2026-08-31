"""Normalize legacy single-run results into the unified report contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .metrics import curve_rows, drawdown_rows, performance_metrics, side_metrics
from .models import canonical_hash
from .reporting import render_backtest_html


def write_backtest_report(
    result: Any,
    path: str | Path,
    *,
    strategy_version: str,
    comparison: Mapping[str, Any] | None = None,
) -> Path:
    trades = result.trades.copy()
    orders = result.orders.copy()
    metrics, unavailable = performance_metrics(result.equity_curve, trades, orders)
    trade_rows = _records(trades)
    order_rows = _records(orders)
    symbols = sorted(
        set(trades.get("symbol", pd.Series(dtype=str)).dropna().astype(str))
        | set(orders.get("symbol", pd.Series(dtype=str)).dropna().astype(str))
    )
    combination_id = "组合-单次回测"
    equity_rows = curve_rows(result.equity_curve)
    execution_hash = canonical_hash({
        "orders": order_rows, "trades": trade_rows, "equity_curve": equity_rows,
    })
    payload = {
        "schema_version": "2", "run_id": "legacy-single-run",
        "report_date": pd.Timestamp.now(tz="UTC").date().isoformat(),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": {
            "schema_version": "2", "source_run_id": "legacy-single-run",
            "strategy_id": "turtle", "strategy_version": strategy_version,
            "code_version": "unknown", "source_hash": "legacy",
            "instruments": [
                {"symbol": symbol, "timeframe": "D1", "dataset_version": "unknown"}
                for symbol in symbols
            ],
        },
        "plan": {
            "schema_version": "2", "strategy_id": "turtle", "symbols": symbols,
            "parameter_space": {}, "constraints": [],
        },
        "combinations": [{
            "schema_version": "2", "combination_id": combination_id,
            "parameters": {}, "status": "COMPLETED", "qualified": True,
            "rank": 1, "score": None, "metrics": metrics,
            "validation_metrics": metrics, "holdout_metrics": {}, "folds": [],
            "long_metrics": side_metrics(trades, "long"),
            "short_metrics": side_metrics(trades, "short"),
            "equity_curve": equity_rows,
            "drawdown_curve": drawdown_rows(result.equity_curve),
            "trades": trade_rows, "orders": order_rows,
            "unavailable": unavailable, "overfit_risk": {"level": "NOT_APPLICABLE"},
            "execution_result_hash": execution_hash,
        }],
        "validation_status": "SINGLE_RUN",
        "best_combination_id": combination_id,
        "stable_combination_ids": [],
        "comparison_assumptions_hash": canonical_hash(comparison or {}),
        "result_hash": execution_hash,
        "conclusion": {
            "status": "SINGLE_RUN_DIAGNOSTIC",
            "summary": "该报告为单组参数诊断，不包含参数选优或独立留出确认。",
            "key_issues": ([{
                "code": "LEGACY_COMPARISON", "severity": "INFO",
                "message": json.dumps(comparison, ensure_ascii=False, sort_keys=True),
            }] if comparison else []),
        },
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_backtest_html(payload), encoding="utf-8")
    return target


def _records(frame: pd.DataFrame) -> list[Mapping[str, Any]]:
    if frame.empty:
        return []
    return json.loads(frame.to_json(orient="records", date_format="iso"))


__all__ = ["write_backtest_report"]
