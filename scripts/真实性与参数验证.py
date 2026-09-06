"""Frozen local spot-data experiment. No downloads, live orders or holdout tuning."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--label", required=True)
    parser.add_argument("--cost-multiplier", type=float, default=1.)
    args = parser.parse_args()
    assert (args.source_root / "src/inv_trend/__init__.py").is_file(), "source snapshot missing"
    sys.path.insert(0, str(args.source_root.resolve() / "src"))
    import inv_trend
    assert Path(inv_trend.__file__).resolve().is_relative_to(args.source_root.resolve())
    import pandas as pd
    import yaml
    from inv_trend.application.backtest import BacktestBatchService, BacktestPlan
    from inv_trend.application.backtest.models import BacktestSourceBundle, SourceInstrument
    from inv_trend.application.backtest.reporting import render_backtest_html
    from inv_trend.config import load_strategy_mapping

    output = ROOT / "优化验证" / args.label
    output.mkdir(parents=True, exist_ok=True)
    plan_path = ROOT / "config/真实性审计参数计划.yaml"
    plan = BacktestPlan.from_mapping(yaml.safe_load(plan_path.read_text(encoding="utf-8")))
    data, instruments, evidence = {}, [], {}
    for symbol in plan.symbols:
        path = ROOT / f"processed_data/cleaned/metal_tech_core/{symbol.lower()}usdt_binance_d1_cleaned.csv"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        raw = pd.read_csv(path)
        frame = raw.set_index(pd.to_datetime(raw["date"], utc=True))[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)
        frame.index.name = "date"
        assert frame.index.is_unique and frame.index.is_monotonic_increasing
        assert len(frame.index) == len(pd.date_range(frame.index[0], frame.index[-1], freq="D"))
        frame.attrs["dataset_version"] = digest
        data[symbol] = frame
        instruments.append(SourceInstrument(symbol, f"{symbol}USDT.BINANCE.SPOT", "D1", digest,
                                            digest, "research-not-daily-screened", "research-not-live"))
        evidence[symbol] = dict(path=str(path.relative_to(ROOT)), sha256=digest, bars=len(frame),
                                start=frame.index[0].isoformat(), end=frame.index[-1].isoformat())
    assert data["BTC"].index.equals(data["ETH"].index)
    config = load_strategy_mapping()
    config.setdefault("risk", {})["contracts"] = {
        symbol: dict(can_long=True, can_short=False, qty_step=.001, min_qty=.001,
                     cost_bps=10. * args.cost_multiplier, slippage_bps=5. * args.cost_multiplier)
        for symbol in plan.symbols
    }
    source = BacktestSourceBundle("frozen-local-spot-audit", "local-research", "turtle-audit",
                                  tuple(instruments), config, code_version=_source_digest(args.source_root))
    frozen = dict(data=evidence, plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                  plan=plan.to_dict(), fee_bps=10 * args.cost_multiplier,
                  slippage_bps=5 * args.cost_multiplier, source_root=str(args.source_root),
                  code_sha256=source.code_version, python=sys.version, pandas=pd.__version__,
                  selection="validation only; holdout is confirmation, never parameter fallback")
    (output / "实验锁定.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    started = perf_counter()
    result = BacktestBatchService().run(source, plan, data, run_id="reproducible-audit",
                                       now=datetime(2026, 9, 7, tzinfo=timezone.utc))
    elapsed = perf_counter() - started
    assert all(item.status == "COMPLETED" for item in result.combinations), [
        item.error for item in result.combinations if item.status != "COMPLETED"
    ]
    payload = result.to_dict()
    (output / "完整回测结果.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    start = perf_counter()
    html = render_backtest_html(result)
    render_seconds = perf_counter() - start
    (output / "参数与交易报告.html").write_text(html, encoding="utf-8")
    summary = dict(seconds=elapsed, render_seconds=render_seconds, html_bytes=len(html.encode()),
                   result_hash=payload.get("result_hash"), best=result.best_combination_id,
                   stable=result.stable_combination_ids, validation_status=result.validation_status,
                   combinations=[dict(id=x.combination_id, parameters=x.parameters, rank=x.rank,
                                      metrics=x.metrics, oos=x.validation_metrics, holdout=x.holdout_metrics)
                                 for x in result.combinations])
    (output / "对比摘要.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(label=args.label, seconds=elapsed, status=result.validation_status,
                         combinations=len(result.combinations), best=result.best_combination_id)))


def _source_digest(root):
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*")):
        if path.suffix in {".py", ".js", ".css", ".yaml"}:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    main()
