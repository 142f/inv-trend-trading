"""Read-only source audit, frozen-input regressions, benchmarks and EMA research.

Run with the repository venv: python scripts/EMA仓位五轮验证.py --stage research
Source data is opened with SQLite mode=ro. All artifacts go to --output.
"""
from __future__ import annotations

import argparse
import cProfile
from dataclasses import replace
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import pstats
import sqlite3
import statistics
import sys
from time import perf_counter
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--stage", choices=("freeze", "reference", "benchmark", "research"), required=True)
parser.add_argument("--output", type=Path, default=ROOT / "outputs/EMA仓位五轮审查")
parser.add_argument("--baseline", action="store_true")
args = parser.parse_args()
sys.path.insert(0, str(args.output / "基线源码" if args.baseline else ROOT / "src"))

import numpy as np
import pandas as pd

from inv_trend.application.EMA统一基准 import prepare, run, common_specs
from inv_trend.core.EMA趋势策略 import Config
from inv_trend.core.执行约束 import ExecutionPolicy


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str,
                               allow_nan=False), encoding="utf-8")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def freeze():
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if (out / "数据协议.json").exists():
        raise FileExistsError("frozen input already exists")
    db_path = ROOT / "data/metadata/研究目录_v3.sqlite3"
    db_hash = sha(db_path)
    frames, lineage = {}, {}
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        for symbol in ("BTC", "ETH"):
            rows = db.execute("""SELECT v.*,o.path,o.stored_sha256 FROM dataset_heads h
                JOIN dataset_versions v USING(version)
                JOIN data_aliases a ON a.logical_path=v.artifact_path
                JOIN data_objects o USING(object_id)
                WHERE h.symbol=? AND h.timeframe='D1' AND h.channel='curated'""", (symbol,)).fetchall()
            if len(rows) != 1:
                raise ValueError("ambiguous pinned dataset")
            item = dict(rows[0])
            source = (ROOT / "data" / item["path"]).resolve()
            if not source.is_relative_to((ROOT / "data").resolve()) or sha(source) != item["stored_sha256"]:
                raise ValueError("invalid source path/hash")
            frames[symbol] = pd.read_parquet(source).rename(
                columns={"data_source": "source", "bar_end": "available_at"})
            lineage[symbol] = dict(version=item["version"], sha256=item["stored_sha256"],
                instrument=item["instrument_id"], source=str(source), limitations=[
                "spot history used as derivative research proxy", "USDT/USD=1 assumption",
                "no historical funding, margin tiers or liquidation matching"])
        sources = db.execute("SELECT DISTINCT dataset_id FROM market_bars_v3 WHERE symbol='XAUUSD_DUKAS'").fetchall()
        if len(sources) != 1:
            raise ValueError("ambiguous XAU dataset")
        f = pd.read_sql_query("SELECT * FROM market_bars_v3 WHERE dataset_id=? AND symbol=? ORDER BY ordinal",
                              db, params=(sources[0][0], "XAUUSD_DUKAS"))
        f["timestamp"] = pd.to_datetime(f.date, utc=True)
        f["available_at"] = f.timestamp + pd.Timedelta(days=1)
        frames["XAU"] = f
        lineage["XAU"] = dict(dataset_id=sources[0][0], limitations=[
            "provider date + 1 UTC day assumption", "bid-only proxy; historical spread/calendar unverified",
            "tick volume is not executable contract volume; participation model disabled",
            "contract multiplier=1 and quantity step=1e-6 are research assumptions"])
    from inv_trend.core.EMA趋势策略 import signals
    for symbol, frame in frames.items():
        signals(frame)  # Validate full source before freezing the evaluation subset.
        frame.to_parquet(out / f"冻结行情_{symbol}.parquet", index=False)
        lineage[symbol].update(rows=len(frame), start=str(frame.timestamp.min()),
                              end=str(frame.timestamp.max()), frozen_sha256=sha(out / f"冻结行情_{symbol}.parquet"))
    assert db_hash == sha(db_path), "source database changed during audit"
    common_start = max(pd.to_datetime(f.timestamp, utc=True).iloc[165] for f in frames.values())
    start = max(pd.Timestamp("2020-01-01", tz="UTC"), common_start)
    end = min(pd.to_datetime(f.available_at, utc=True).max() for f in frames.values())
    write(out / "数据协议.json", dict(data=lineage, source_database_sha256=db_hash,
        start=str(start), end=str(end), status="RESEARCH_ASSUMPTION", model="fixed EMA; no parameter search",
        validation="chronological yearly diagnostics, continuous account; historically inspected, not pristine OOS",
        costs="5bps fee + 5bps adverse slippage; 3% annual financing proxy + 3% short carry",
        io=dict(sql_queries=4, parquet_reads=2, source_rows=sum(len(f) for f in frames.values()))))
    print(json.dumps({s: {k: v for k,v in x.items() if k in ('rows','start','end')} for s,x in lineage.items()}))


def inputs():
    protocol = json.loads((args.output / "数据协议.json").read_text(encoding="utf-8"))
    frames = {}
    for symbol, item in protocol["data"].items():
        path = args.output / f"冻结行情_{symbol}.parquet"
        if sha(path) != item["frozen_sha256"]:
            raise ValueError("frozen input changed")
        frames[symbol] = pd.read_parquet(path)
    return frames, protocol


def tables(result):
    return {name: getattr(result, name) for name in (
        "equity_curve", "orders", "trade_details", "open_trade_details", "decisions", "cash_ledger")}


def reference():
    frames, protocol = inputs()
    data = prepare({s: frames[s] for s in ("BTC", "ETH")})
    summary, result = run(data, Config(), protocol["start"], "2021-01-01")
    out = args.output / ("原始行为" if args.baseline else "兼容行为")
    out.mkdir(exist_ok=True)
    for name, frame in tables(result).items():
        frame.to_pickle(out / f"{name}.pkl")
        if not args.baseline:
            expected = pd.read_pickle(args.output / "原始行为" / f"{name}.pkl")
            assertion = pd.testing.assert_series_equal if isinstance(frame, pd.Series) else pd.testing.assert_frame_equal
            assertion(frame, expected, rtol=1e-12, atol=1e-10)
    write(out / "汇总.json", summary["metrics"])
    print("reference equivalence passed" if not args.baseline else "baseline recorded", flush=True)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def benchmark():
    frames, _ = inputs()
    core = module(args.output / "基线源码/inv_trend/core/EMA趋势策略.py", "baseline_core")
    app = module(args.output / "基线源码/inv_trend/application/EMA统一基准.py", "baseline_app")
    app.Config, app.signals = core.Config, core.signals
    old, new = lambda: app.prepare(frames), lambda: prepare(frames)
    expected, actual = old(), new()
    for s in frames: pd.testing.assert_frame_equal(expected[s], actual[s])
    samples = [[], []]
    for i in range(7):
        for j in ((0, 1) if i % 2 == 0 else (1, 0)):
            gc.collect()
            started = perf_counter()
            (old, new)[j]()
            samples[j].append(perf_counter() - started)
    peaks = []
    for operation in (old, new):
        gc.collect()
        tracemalloc.start()
        operation()
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
    report = dict(scope="same complete BTC/ETH/XAU prepare outputs; disk IO outside timed region",
        python=platform.python_version(), pandas=pd.__version__, numpy=np.__version__,
        samples_seconds=samples, median_seconds=list(map(statistics.median, samples)), peak_python_bytes=peaks,
        speedup=statistics.median(samples[0])/statistics.median(samples[1]), exact_frame_equality=True,
        indicator_passes_before=4, indicator_passes_after=1, relation_passes_before=4, relation_passes_after=4,
        io_before=0, io_after=0, complexity="O(k*n) -> O(n+k*n); k=4 constant, both O(n)")
    write(args.output / "性能对比.json", report)
    print(json.dumps(report), flush=True)


def independent_check(result):
    # Recompute each close NAV from independently exported asset attribution and costs.
    ledger = result.cash_ledger
    totals = result.attribution.groupby("time").total_pnl.sum()
    np.testing.assert_allclose(ledger.equity, 100000 + totals.reindex(ledger.time).to_numpy(), rtol=1e-10, atol=1e-6)
    orders = result.orders
    if not orders.empty:
        status = orders.get("status", pd.Series("filled", index=orders.index)).fillna("filled")
        fills = orders[status.isin(["filled", "partial"])]
        # Independent cash recurrence for derivative account: principal is never debited.
        realized = result.trades.gross_pnl.sum() if not result.trades.empty else 0.
        fees = fills.cost.sum()
        expected_cash = 100000 + realized - fees - result.carry_cost
        np.testing.assert_allclose(ledger.cash.iloc[-1], expected_cash, atol=1e-6, rtol=1e-10)
        np.testing.assert_allclose(ledger.fee_cost_cumulative.iloc[-1], fees, atol=1e-6)
    return dict(each_close_nav=True, final_cash=True, cumulative_fees=True)


def research():
    frames, protocol = inputs()
    prepared = prepare(frames)
    cfg = Config(cap=1., risk=.20, position_mode="confirmed")
    policy = ExecutionPolicy(max_gross_leverage=2., financing_bps_per_year=300.,
        volume_asset_classes=("equity", "crypto"), reduce_excess_leverage=True)
    outputs = []
    for group, symbols in (("第四轮_BTC_ETH组合", ("BTC", "ETH")), ("第四轮_XAU独立", ("XAU",))):
        data = {s: prepared[s] for s in symbols}
        specs = {s: replace(spec, asset_class="crypto" if s != "XAU" else "research_proxy",
                            max_symbol_leverage=2.) for s,spec in common_specs(symbols).items()}
        scenarios = (("基础", cfg, policy, 1, 1), ("综合成本翻倍", cfg,
            replace(policy, financing_bps_per_year=600., short_borrow_bps_per_year=600.), 1, 2),
            ("延迟两根", cfg, policy, 2, 1), ("流动性收紧", cfg,
            replace(policy, volume_participation=.001), 1, 1),
            ("原EMA", Config(), replace(policy, max_gross_leverage=.9, reduce_excess_leverage=False), 1, 1),
            ("海龟", Config(), replace(policy, max_gross_leverage=.9, reduce_excess_leverage=False), 1, 1))
        for label, config, execution, delay, cost in scenarios:
            # Gold tick volume cannot measure contracts: no artificial liquidity claim.
            if group.endswith("XAU独立") and label == "流动性收紧": continue
            scenario_specs = {s: replace(spec, cost_bps=5*cost, slippage_bps=5*cost,
                max_symbol_leverage=(.225 if label in ("原EMA", "海龟") else 2.)) for s,spec in specs.items()}
            profiler = cProfile.Profile()
            started = perf_counter()
            profiler.enable()
            summary, result = run(data, config, protocol["start"], protocol["end"],
                kind="turtle" if label == "海龟" else "ema", execution_policy=execution,
                signal_delay_bars=delay, asset_specs=scenario_specs)
            profiler.disable()
            elapsed = perf_counter() - started
            check = independent_check(result)
            folder = args.output / group / label
            folder.mkdir(parents=True, exist_ok=True)
            for name, frame in {**tables(result), "asset_attribution": result.attribution}.items():
                frame.to_pickle(folder / f"{name}.pkl")
            with (folder / "性能剖析.txt").open("w", encoding="utf-8") as stream:
                pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(25)
            decisions = result.decisions
            diagnostic = {}
            if "target_weight" in decisions:
                limited = decisions.risk_reasons.apply(
                    lambda values: bool(set(values) & {"stop_risk", "quality", "short_scale"}))
                diagnostic = dict(risk_limited_decisions=int(limited.sum()), total_decisions=len(decisions),
                    confirmed_decisions=int((decisions.direction != 0).sum()))
            years = {}
            for year, values in summary["returns"].groupby(summary["returns"].index.year):
                years[str(year)] = float((1 + values).prod()-1)
            report = dict(group=group, scenario=label, elapsed_profiled_seconds=elapsed,
                metrics=summary["metrics"], ledger_check=check, diagnostic=diagnostic,
                yearly_returns=years, decisions=len(decisions), orders=len(result.orders),
                exposure_quantiles=result.cash_ledger.gross_leverage.quantile([0,.5,.9,1]).to_dict())
            write(folder / "回测汇总.json", report)
            outputs.append(report)
            write(args.output / "回测对比.json", outputs)
            print(group, label, "seconds", round(elapsed, 2), "net", round(report["metrics"]["total"], 4), flush=True)


if __name__ == "__main__":
    {"freeze": freeze, "reference": reference, "benchmark": benchmark, "research": research}[args.stage]()
