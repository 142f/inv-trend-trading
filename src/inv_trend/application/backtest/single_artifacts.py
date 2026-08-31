"""One-release writer for legacy single-run callers using the unified renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .legacy_report import write_backtest_report


def write_single_backtest_outputs(
    result: Any,
    out_dir: str | Path,
    *,
    strategy_version: str = "corrected-v2",
    html: bool = True,
    comparison: Mapping[str, Any] | None = None,
) -> Path:
    """Write compatibility files from one central implementation."""

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    result.equity_curve.to_csv(target / "equity_curve.csv")
    result.orders.to_csv(target / "orders.csv", index=False)
    result.trades.to_csv(target / "trades.csv", index=False)
    result.trade_details.to_csv(target / "trade_details.csv", index=False)
    (target / "metrics.json").write_text(
        json.dumps(result.metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if html:
        write_backtest_report(
            result, target / "report.html", strategy_version=strategy_version,
            comparison=comparison,
        )
    return target


__all__ = ["write_single_backtest_outputs"]
